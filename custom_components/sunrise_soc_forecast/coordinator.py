"""Data update coordinator for Sunrise SoC Forecast."""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.sun import get_astral_event_next
from homeassistant.util import dt as dt_util

from .calculator import (
    BatteryConfig,
    BackupConfig,
    ConsumptionData,
    DayResult,
    get_consumption,
    get_overnight_params,
    predict_day1_daytime,
    predict_future_day,
)
from .const import (
    CONF_MAIN_SOC_ENTITY,
    CONF_MAIN_POWER_ENTITY,
    CONF_MAIN_CAPACITY,
    CONF_MAIN_FLOOR,
    CONF_BACKUP_ENABLED,
    CONF_BACKUP_SOC_ENTITY,
    CONF_BACKUP_DISCHARGE_ENTITY,
    CONF_BACKUP_CAPACITY,
    CONF_BACKUP_FLOOR,
    CONF_BACKUP_DISCHARGE_KW,
    CONF_BACKUP_ACTIVATION_HOUR,
    CONF_BACKUP_MODE,
    CONF_BACKUP_CHARGE_EFFICIENCY,
    CONF_BACKUP_DISCHARGE_EFFICIENCY,
    CONF_MAIN_INVERTER_EFFICIENCY,
    BACKUP_MODE_ALWAYS,
    CONF_DEFAULT_DAILY,
    CONF_DEFAULT_OVERNIGHT,
    CONF_GUARD_THRESHOLD,
    CONF_FORECAST_DAYS,
    CONF_SOLCAST_REMAINING,
    CONF_SOLCAST_FORECAST_TODAY,
    CONF_SOLAR_ENTITY_REMAINING,
    CONF_GRID_POWER_ENTITY,
    CONF_TARGET_SOC,
    DEFAULT_TARGET_SOC,
    SOLCAST_STANDARD,
    SOLCAST_SHIFTED,
    SOLAR_DAY_MAP,
    DEFAULT_MAIN_CAPACITY,
    DEFAULT_MAIN_FLOOR,
    DEFAULT_BACKUP_CAPACITY,
    DEFAULT_BACKUP_FLOOR,
    DEFAULT_BACKUP_DISCHARGE_KW,
    DEFAULT_BACKUP_ACTIVATION_HOUR,
    STORAGE_VERSION,
    DEFAULT_DAILY_CONSUMPTION,
    DEFAULT_OVERNIGHT_CONSUMPTION,
    DEFAULT_GUARD_THRESHOLD,
    DEFAULT_FORECAST_DAYS,
    DEFAULT_MAIN_INVERTER_EFFICIENCY,
    DEFAULT_BACKUP_CHARGE_EFFICIENCY,
    DEFAULT_BACKUP_DISCHARGE_EFFICIENCY,
)

_LOGGER = logging.getLogger(__name__)

# Cache TTL for astral calculations (seconds)
_ASTRAL_CACHE_TTL = 60


class SunriseSocCoordinator:
    """Coordinator for Sunrise SoC Forecast."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any], entry_id: str = "default") -> None:
        """Initialize."""
        self.hass = hass
        self.config = config
        self._entry_id = entry_id

        self.main = BatteryConfig(
            capacity_kwh=max(0.1, config.get(CONF_MAIN_CAPACITY, DEFAULT_MAIN_CAPACITY)),
            floor_percent=config.get(CONF_MAIN_FLOOR, DEFAULT_MAIN_FLOOR),
        )
        self.backup = BackupConfig(
            enabled=config.get(CONF_BACKUP_ENABLED, False),
            capacity_kwh=config.get(CONF_BACKUP_CAPACITY, DEFAULT_BACKUP_CAPACITY),
            floor_percent=config.get(CONF_BACKUP_FLOOR, DEFAULT_BACKUP_FLOOR),
            fixed_discharge_kw=config.get(CONF_BACKUP_DISCHARGE_KW, DEFAULT_BACKUP_DISCHARGE_KW),
            activation_hour=config.get(CONF_BACKUP_ACTIVATION_HOUR, DEFAULT_BACKUP_ACTIVATION_HOUR),
            mode=config.get(CONF_BACKUP_MODE, BACKUP_MODE_ALWAYS),
        )

        self.num_days = config.get(CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS)
        self.target_soc = config.get(CONF_TARGET_SOC, DEFAULT_TARGET_SOC)

        # Inverter efficiency (stored as decimal, e.g., 0.94)
        self.inverter_efficiency = config.get(
            CONF_MAIN_INVERTER_EFFICIENCY, DEFAULT_MAIN_INVERTER_EFFICIENCY
        ) / 100.0
        self.backup_charge_efficiency = config.get(
            CONF_BACKUP_CHARGE_EFFICIENCY, DEFAULT_BACKUP_CHARGE_EFFICIENCY
        ) / 100.0
        self.backup_discharge_efficiency = config.get(
            CONF_BACKUP_DISCHARGE_EFFICIENCY, DEFAULT_BACKUP_DISCHARGE_EFFICIENCY
        ) / 100.0

        # Internal consumption tracking — 24 hourly buckets
        # Each hour has a 7-day rolling average
        self._hourly_history: list[deque[float]] = [deque(maxlen=7) for _ in range(24)]
        self._hourly_energy_kwh: list[float] = [0.0] * 24
        self._current_hour: int | None = None

        # Legacy daily/overnight totals (derived from hourly for backward compatibility)
        self._daily_history: deque[float] = deque(maxlen=7)
        self._overnight_history: deque[float] = deque(maxlen=7)

        # Internal energy accumulators
        self._daily_energy_kwh: float = 0.0
        self._overnight_energy_kwh: float = 0.0
        self._grid_energy_today_kwh: float = 0.0
        self._last_power_reading: float | None = None
        self._last_power_time: datetime | None = None
        self._last_grid_reading: float | None = None
        self._last_grid_time: datetime | None = None
        self._was_overnight: bool = False

        # Frozen Solcast — captured at sunset to prevent midnight shift
        # Stores both total and hourly arrays per day
        self._frozen_solcast: dict[int, float] = {}
        self._frozen_solcast_hourly: dict[int, list[float]] = {}

        # Current results
        self.results: dict[int, DayResult] = {}

        # Callback set with unregister support (fix #3)
        self._update_callbacks: set = set()

        # Persistent storage
        # Store(hass, 1, ...) = HA storage framework schema version
        # STORAGE_VERSION in const.py = app-level version for invalidating stale frozen data
        self._store = Store(hass, 1, f"sunrise_soc_forecast_{entry_id}")

        # Astral cache (fix #4)
        self._astral_cache: dict[str, Any] = {}
        self._astral_cache_time: float = 0

        # Unsubs for event listeners (populated by __init__.py)
        self.unsubs: list = []

    def _get_astral(self) -> tuple:
        """Get sunrise/sunset with caching to avoid redundant astral calls."""
        now = time.monotonic()
        if now - self._astral_cache_time < _ASTRAL_CACHE_TTL and self._astral_cache:
            return self._astral_cache.get("sunrise"), self._astral_cache.get("sunset")

        sunrise = get_astral_event_next(self.hass, "sunrise")
        sunset = get_astral_event_next(self.hass, "sunset")
        self._astral_cache = {"sunrise": sunrise, "sunset": sunset}
        self._astral_cache_time = now
        return sunrise, sunset

    @property
    def is_overnight(self) -> bool:
        """Check if it's currently overnight (sunset to sunrise)."""
        sunrise, sunset = self._get_astral()
        if sunrise is None or sunset is None:
            return False
        return sunrise < sunset

    def get_state_float(self, entity_id: str, default: float = 0.0) -> float:
        """Get a sensor state as float."""
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    def accumulate_energy(self) -> None:
        """Accumulate energy from the house load power sensor."""
        now = dt_util.now()
        current_hour = now.hour
        power_w = self.get_state_float(self.config[CONF_MAIN_POWER_ENTITY])
        power_kw = power_w / 1000.0
        overnight = self.is_overnight

        # Initialize current hour on first call
        if self._current_hour is None:
            self._current_hour = current_hour

        # Detect hour change — record the completed hour and save
        if current_hour != self._current_hour:
            prev_hour = self._current_hour
            if self._hourly_energy_kwh[prev_hour] > 0:
                self._hourly_history[prev_hour].append(
                    round(self._hourly_energy_kwh[prev_hour], 3)
                )
            self._hourly_energy_kwh[prev_hour] = 0.0
            self._current_hour = current_hour
            self.hass.async_create_task(self.async_save())

        if self._last_power_reading is not None and self._last_power_time is not None:
            elapsed_hours = (now - self._last_power_time).total_seconds() / 3600
            if elapsed_hours > 0 and elapsed_hours < 1:
                avg_kw = (self._last_power_reading + power_kw) / 2
                energy_kwh = avg_kw * elapsed_hours
                self._daily_energy_kwh += energy_kwh
                self._hourly_energy_kwh[current_hour] += energy_kwh
                if overnight:
                    self._overnight_energy_kwh += energy_kwh

        # Detect sunrise transition
        if self._was_overnight and not overnight:
            self._on_sunrise()

        # Detect sunset transition
        if not self._was_overnight and overnight:
            self._on_sunset()

        self._last_power_reading = power_kw
        self._last_power_time = now
        self._was_overnight = overnight

    def accumulate_grid(self) -> None:
        """Accumulate energy from the grid power sensor."""
        grid_entity = self.config.get(CONF_GRID_POWER_ENTITY, "")
        if not grid_entity:
            return

        now = dt_util.now()
        power_w = self.get_state_float(grid_entity)
        power_kw = max(0, power_w / 1000.0)

        if self._last_grid_reading is not None and self._last_grid_time is not None:
            elapsed_hours = (now - self._last_grid_time).total_seconds() / 3600
            if elapsed_hours > 0 and elapsed_hours < 1:
                avg_kw = (self._last_grid_reading + power_kw) / 2
                self._grid_energy_today_kwh += avg_kw * elapsed_hours

        self._last_grid_reading = power_kw
        self._last_grid_time = now

    def _on_sunrise(self) -> None:
        """Handle sunrise: record overnight consumption, reset accumulator."""
        if self._overnight_energy_kwh > 0:
            self._overnight_history.append(round(self._overnight_energy_kwh, 2))
            _LOGGER.info(
                "Overnight consumption recorded: %.2f kWh (history: %s)",
                self._overnight_energy_kwh,
                list(self._overnight_history),
            )
        self._overnight_energy_kwh = 0.0
        self.hass.async_create_task(self.async_save())

    def _on_sunset(self) -> None:
        """Handle sunset: freeze Days 2-7."""
        self.freeze()
        self.hass.async_create_task(self.async_save())

    def on_midnight(self) -> None:
        """Handle midnight: record daily consumption, reset accumulators."""
        if self._daily_energy_kwh > 0:
            self._daily_history.append(round(self._daily_energy_kwh, 2))
            _LOGGER.info(
                "Daily consumption recorded: %.2f kWh (history: %s)",
                self._daily_energy_kwh,
                list(self._daily_history),
            )
        self._daily_energy_kwh = 0.0
        self._grid_energy_today_kwh = 0.0
        self.hass.async_create_task(self.async_save())

    def get_consumption(self) -> ConsumptionData:
        """Get current consumption averages with fallback."""
        daily_avg = None
        overnight_avg = None

        if self._daily_history:
            daily_avg = sum(self._daily_history) / len(self._daily_history)
        if self._overnight_history:
            overnight_avg = sum(self._overnight_history) / len(self._overnight_history)

        return get_consumption(
            daily_avg=daily_avg,
            overnight_avg=overnight_avg,
            default_daily=self.config.get(CONF_DEFAULT_DAILY, DEFAULT_DAILY_CONSUMPTION),
            default_overnight=self.config.get(CONF_DEFAULT_OVERNIGHT, DEFAULT_OVERNIGHT_CONSUMPTION),
            guard_threshold=self.config.get(CONF_GUARD_THRESHOLD, DEFAULT_GUARD_THRESHOLD),
        )

    def get_hourly_averages(self) -> list[float]:
        """Get 24-hour consumption averages (kWh per hour).

        Returns a list of 24 floats, one per hour (0=midnight, 23=11pm).
        Hours without data use a flat fallback derived from daily/overnight averages.
        """
        consumption = self.get_consumption()
        overnight_hours = 12.0  # approximate
        sunrise, sunset = self._get_astral()
        if sunrise is not None and sunset is not None:
            overnight_hours = abs((sunrise - sunset).total_seconds() / 3600)
            if overnight_hours <= 0:
                overnight_hours = 12.0
        daytime_hours = 24.0 - overnight_hours

        # Flat fallback rates
        overnight_rate = consumption.avg_overnight_kwh / overnight_hours if overnight_hours > 0 else 2.67
        daytime_rate = (consumption.avg_daily_kwh - consumption.avg_overnight_kwh) / daytime_hours if daytime_hours > 0 else 5.0
        if daytime_rate < 0:
            daytime_rate = 0.0

        # Determine which hours are daytime vs overnight using actual astral data
        sunset_h = 18
        sunrise_h = 6
        if sunset is not None:
            sl = sunset.astimezone()
            sunset_h = sl.hour
        if sunrise is not None:
            rl = sunrise.astimezone()
            sunrise_h = rl.hour

        averages = []
        for h in range(24):
            if self._hourly_history[h]:
                # Use real data
                averages.append(
                    round(sum(self._hourly_history[h]) / len(self._hourly_history[h]), 3)
                )
            else:
                # Fallback: flat rate based on whether this hour is overnight
                # Overnight = sunset_h to sunrise_h (wrapping through midnight)
                if sunset_h > sunrise_h:
                    # Normal case: sunset 18, sunrise 6 → night = 18-23, 0-5
                    is_night = h >= sunset_h or h < sunrise_h
                else:
                    # Unusual case: sunset < sunrise (polar regions)
                    is_night = sunset_h <= h < sunrise_h
                averages.append(round(overnight_rate if is_night else daytime_rate, 3))

        return averages

    def get_solar_kwh(self, day: int) -> float:
        """Get solar daily total for a day."""
        entity_id = self._get_solar_entity_id(day)
        return self.get_state_float(entity_id)

    # Keep old name as alias
    def get_solcast_kwh(self, day: int) -> float:
        """Backward-compatible alias."""
        return self.get_solar_kwh(day)

    def _get_solar_entity_id(self, day: int) -> str:
        """Get the solar forecast entity ID for a given day.

        Checks auto-discovered keys first, then falls back to legacy Solcast keys.
        Handles post-midnight shift for legacy Solcast mapping.
        """
        from .const import SOLAR_DAY_MAP

        # Try auto-discovered entity first
        solar_conf = SOLAR_DAY_MAP.get(day)
        if solar_conf:
            entity_id = self.config.get(solar_conf, "")
            if entity_id:
                return entity_id

        # Fall back to legacy Solcast keys
        if day == 1:
            return self.config.get(CONF_SOLCAST_FORECAST_TODAY, "")
        now = dt_util.now()
        post_midnight = self.is_overnight and now.hour < 12
        mapping = SOLCAST_SHIFTED if post_midnight else SOLCAST_STANDARD
        conf_key = mapping.get(day)
        if conf_key is None:
            return ""
        return self.config.get(conf_key, "")

    # Keep old name as alias
    def _get_solcast_entity_id(self, day: int) -> str:
        """Backward-compatible alias."""
        return self._get_solar_entity_id(day)

    def get_solar_hourly(self, day: int) -> list[float] | None:
        """Get solar forecast as a time-series array.

        Returns:
            96-element list (kWh per 15 min) for Open-Meteo watts data
            48-element list (kWh per 30 min) for Solcast detailedForecast
            24-element list (kWh per hour) for Solcast detailedHourly or Open-Meteo wh_period
            None if no hourly data available

        The calculator handles all three lengths with appropriate step sizes.
        """
        entity_id = self._get_solar_entity_id(day)
        if not entity_id:
            return None

        state = self.hass.states.get(entity_id)
        if state is None:
            return None

        attrs = state.attributes

        # Open-Meteo: watts attribute (96 quarter-hourly entries in W)
        watts = attrs.get("watts")
        if watts and isinstance(watts, dict) and len(watts) >= 90:
            quarter_hourly = [0.0] * 96
            for ts_str, w_val in watts.items():
                try:
                    hour, minute = self._parse_time_from_key(ts_str)
                    idx = hour * 4 + minute // 15
                    if 0 <= idx < 96:
                        quarter_hourly[idx] = float(w_val) / 1000.0 * 0.25  # W → kWh per 15 min
                except (ValueError, TypeError, IndexError):
                    continue
            if any(v > 0 for v in quarter_hourly):
                return quarter_hourly

        # Solcast: detailedForecast (48 half-hour entries in kW)
        detailed = attrs.get("detailedForecast")
        if detailed and isinstance(detailed, list):
            half_hourly = [0.0] * 48
            for entry in detailed:
                try:
                    period = entry.get("period_start")
                    pv = float(entry.get("pv_estimate", 0))
                    if hasattr(period, "hour"):
                        hour = period.hour
                        minute = period.minute
                    elif isinstance(period, str) and "T" in period:
                        time_part = period.split("T")[1][:5]
                        hour = int(time_part[:2])
                        minute = int(time_part[3:5])
                    else:
                        continue
                    idx = hour * 2 + (1 if minute >= 30 else 0)
                    if 0 <= idx < 48:
                        half_hourly[idx] = pv * 0.5  # kW → kWh per half-hour
                except (ValueError, TypeError, IndexError, AttributeError):
                    continue
            if any(v > 0 for v in half_hourly):
                return half_hourly

        # Open-Meteo: wh_period (24 hourly entries in Wh)
        wh = attrs.get("wh_period")
        if wh and isinstance(wh, dict):
            hourly = [0.0] * 24
            for ts_str, wh_val in wh.items():
                try:
                    hour, _ = self._parse_time_from_key(ts_str)
                    if 0 <= hour < 24:
                        hourly[hour] = float(wh_val) / 1000.0  # Wh → kWh
                except (ValueError, TypeError, IndexError):
                    continue
            if any(v > 0 for v in hourly):
                return hourly

        # Solcast fallback: detailedHourly (24 entries → split into 48 half-hours)
        detailed = attrs.get("detailedHourly")
        if detailed and isinstance(detailed, list):
            half_hourly = [0.0] * 48
            for entry in detailed:
                try:
                    period = entry.get("period_start")
                    pv = float(entry.get("pv_estimate", 0))
                    if hasattr(period, "hour"):
                        hour = period.hour
                    elif isinstance(period, str) and "T" in period:
                        hour = int(period.split("T")[1][:2])
                    else:
                        continue
                    half_hourly[hour * 2] = pv / 2
                    half_hourly[hour * 2 + 1] = pv / 2
                except (ValueError, TypeError, IndexError, AttributeError):
                    continue
            if any(v > 0 for v in half_hourly):
                return half_hourly

        return None

    @staticmethod
    def _parse_time_from_key(ts_str: str) -> tuple[int, int]:
        """Parse hour and minute from an ISO timestamp string key."""
        time_part = ts_str.split("T")[1][:5]
        return int(time_part[:2]), int(time_part[3:5])

    # Keep old name as alias for backward compatibility
    def get_solcast_hourly(self, day: int) -> list[float] | None:
        """Backward-compatible alias for get_solar_hourly."""
        return self.get_solar_hourly(day)

    def update(self) -> None:
        """Recalculate all predictions."""
        now = dt_util.now()
        sunrise, sunset = self._get_astral()

        if sunrise is None or sunset is None:
            return

        overnight = self.is_overnight
        target_kwh = self.target_soc / 100 * self.main.capacity_kwh
        consumption = self.get_consumption()
        hourly_avg = self.get_hourly_averages()

        # Get sunrise/sunset hours for hourly model
        sunrise_local = sunrise.astimezone()
        sunset_local = sunset.astimezone()
        sunrise_hour = sunrise_local.hour + sunrise_local.minute / 60
        sunset_hour = sunset_local.hour + sunset_local.minute / 60

        overnight_params = get_overnight_params(
            sunrise=sunrise,
            sunset=sunset,
            overnight_kwh=consumption.avg_overnight_kwh,
            activation_hour=self.backup.activation_hour,
        )

        # Day 1
        main_soc_pct = self.get_state_float(self.config[CONF_MAIN_SOC_ENTITY])
        if main_soc_pct <= 0:
            # Battery sensor unavailable — keep previous results
            return
        main_kwh = main_soc_pct / 100 * self.main.capacity_kwh

        # Day 1 — unified path for daytime and nighttime
        current_hour_frac = now.hour + now.minute / 60

        if not overnight:
            remaining_entity = self.config.get(
                CONF_SOLAR_ENTITY_REMAINING,
                self.config.get(CONF_SOLCAST_REMAINING, ""),
            )
            remaining_solar = self.get_state_float(remaining_entity)
            solar_hourly_day1 = self.get_solar_hourly(1)

            # Skip update if Solcast data is unavailable (e.g., during API refresh or restart)
            if remaining_solar <= 0 and solar_hourly_day1 is None:
                return

            hours_to_sunset = max(0, (sunset - now).total_seconds() / 3600)
        else:
            remaining_solar = 0.0
            solar_hourly_day1 = None
            hours_to_sunset = 0.0

        total_daytime_hours = 24.0 - overnight_params.overnight_hours

        backup_soc = 0.0
        if self.backup.enabled:
            backup_soc = self.get_state_float(self.config.get(CONF_BACKUP_SOC_ENTITY, ""))

        self.results[1] = predict_day1_daytime(
            current_kwh=main_kwh,
            remaining_solar=remaining_solar,
            hours_to_sunset=hours_to_sunset,
            total_daytime_hours=total_daytime_hours,
            consumption=consumption,
            main=self.main,
            backup=self.backup,
            backup_soc_pct=backup_soc,
            overnight_params=overnight_params,
            target_kwh=target_kwh,
            hourly_consumption=hourly_avg,
            current_hour=current_hour_frac,
            sunset_hour=sunset_hour,
            hourly_solar=solar_hourly_day1,
            inverter_efficiency=self.inverter_efficiency,
            backup_charge_efficiency=self.backup_charge_efficiency,
            backup_discharge_efficiency=self.backup_discharge_efficiency,
        )

        # Days 2-N — always calculate live, only Solcast freezes overnight
        for day in range(2, self.num_days + 1):
            prev_kwh = self.results.get(day - 1, DayResult(0, 0)).predicted_kwh

            # Use frozen solar during overnight to prevent midnight shift
            if overnight and day in self._frozen_solcast and self._frozen_solcast[day] > 0:
                solar = self._frozen_solcast[day]
                solar_hourly = self._frozen_solcast_hourly.get(day)
            elif overnight and now.hour < 12:
                # Post-midnight with no valid frozen data — skip this day
                # to avoid reading shifted entities that have wrong day's data
                if day in self.results:
                    continue
                solar = 0.0
                solar_hourly = None
            else:
                solar = self.get_solar_kwh(day)
                solar_hourly = self.get_solar_hourly(day)

            self.results[day] = predict_future_day(
                prev_kwh=prev_kwh,
                solar_kwh=solar,
                consumption=consumption,
                main=self.main,
                backup=self.backup,
                overnight_params=overnight_params,
                target_kwh=target_kwh,
                hourly_consumption=hourly_avg,
                sunrise_hour=sunrise_hour,
                sunset_hour=sunset_hour,
                hourly_solar=solar_hourly,
                inverter_efficiency=self.inverter_efficiency,
                backup_charge_efficiency=self.backup_charge_efficiency,
                backup_discharge_efficiency=self.backup_discharge_efficiency,
            )

        # Calculate grid needed for each day
        # Uses the worst of: next day's day_low (daytime) and current predicted_kwh (overnight)
        for day in range(1, self.num_days + 1):
            if day in self.results:
                result = self.results[day]
                worst = result.predicted_kwh
                next_day = self.results.get(day + 1)
                if next_day and next_day.morning_low_kwh > 0:
                    worst = min(worst, next_day.morning_low_kwh)
                deficit = target_kwh - worst
                result.grid_needed_kwh = round(max(0, deficit), 2)

        # Notify sensors
        for cb in self._update_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.debug("Error in update callback", exc_info=True)

    def freeze(self) -> None:
        """Freeze solar forecast values at sunset to prevent midnight shift."""
        for day in range(2, self.num_days + 1):
            solar = self.get_solar_kwh(day)
            if solar > 0:
                self._frozen_solcast[day] = solar
            hourly = self.get_solar_hourly(day)
            if hourly:
                self._frozen_solcast_hourly[day] = hourly
        _LOGGER.debug("Frozen solar forecast captured for days 2-%d", self.num_days)

    def register_callback(self, callback_fn) -> None:
        """Register an update callback."""
        self._update_callbacks.add(callback_fn)

    def unregister_callback(self, callback_fn) -> None:
        """Unregister an update callback."""
        self._update_callbacks.discard(callback_fn)

    @property
    def daily_average(self) -> float | None:
        """Get current daily average."""
        if not self._daily_history:
            return None
        return round(sum(self._daily_history) / len(self._daily_history), 2)

    @property
    def overnight_average(self) -> float | None:
        """Get current overnight average."""
        if not self._overnight_history:
            return None
        return round(sum(self._overnight_history) / len(self._overnight_history), 2)

    @property
    def daily_history(self) -> list[float]:
        """Get daily consumption history."""
        return list(self._daily_history)

    @property
    def overnight_history(self) -> list[float]:
        """Get overnight consumption history."""
        return list(self._overnight_history)

    @property
    def daily_energy_today(self) -> float:
        """Get today's accumulated energy so far."""
        return round(self._daily_energy_kwh, 2)

    @property
    def overnight_energy_tonight(self) -> float:
        """Get tonight's accumulated overnight energy so far."""
        return round(self._overnight_energy_kwh, 2)

    @property
    def grid_energy_today(self) -> float:
        """Get today's accumulated grid import energy."""
        return round(self._grid_energy_today_kwh, 2)

    async def async_save(self) -> None:
        """Save state to persistent storage."""
        try:
            data = {
                "storage_version": STORAGE_VERSION,
                "daily_history": list(self._daily_history),
                "overnight_history": list(self._overnight_history),
                "hourly_history": [list(h) for h in self._hourly_history],
                "hourly_energy_kwh": self._hourly_energy_kwh,
                "daily_energy_kwh": self._daily_energy_kwh,
                "overnight_energy_kwh": self._overnight_energy_kwh,
                "grid_energy_today_kwh": self._grid_energy_today_kwh,
                "frozen_solcast": {str(k): v for k, v in self._frozen_solcast.items()},
                "frozen_solcast_hourly": {str(k): v for k, v in self._frozen_solcast_hourly.items()},
            }
            await self._store.async_save(data)
        except Exception:
            _LOGGER.warning("Failed to save state to storage", exc_info=True)

    async def async_load(self) -> None:
        """Load state from persistent storage."""
        try:
            data = await self._store.async_load()
        except Exception:
            _LOGGER.warning("Failed to load state from storage", exc_info=True)
            return

        if data is None:
            _LOGGER.debug("No saved state found")
            return

        # Always restore consumption history and accumulators
        self._daily_history = deque(data.get("daily_history", []), maxlen=7)
        self._overnight_history = deque(data.get("overnight_history", []), maxlen=7)
        self._daily_energy_kwh = data.get("daily_energy_kwh", 0.0)
        self._overnight_energy_kwh = data.get("overnight_energy_kwh", 0.0)
        self._grid_energy_today_kwh = data.get("grid_energy_today_kwh", 0.0)

        # Restore hourly data
        saved_hourly = data.get("hourly_history", [])
        if saved_hourly and len(saved_hourly) == 24:
            self._hourly_history = [deque(h, maxlen=7) for h in saved_hourly]
        saved_hourly_energy = data.get("hourly_energy_kwh", [])
        if saved_hourly_energy and len(saved_hourly_energy) == 24:
            self._hourly_energy_kwh = saved_hourly_energy

        # Restore frozen Solcast values
        frozen_solcast = data.get("frozen_solcast", {})
        for k, v in frozen_solcast.items():
            try:
                self._frozen_solcast[int(k)] = float(v)
            except (TypeError, ValueError):
                pass

        frozen_hourly = data.get("frozen_solcast_hourly", {})
        for k, v in frozen_hourly.items():
            try:
                if isinstance(v, list) and len(v) in (24, 48):
                    self._frozen_solcast_hourly[int(k)] = [float(x) for x in v]
            except (TypeError, ValueError):
                pass

        _LOGGER.info(
            "State restored: %d daily, %d overnight history entries, "
            "%.1f kWh daily accum, %.1f kWh overnight accum, %d frozen Solcast",
            len(self._daily_history),
            len(self._overnight_history),
            self._daily_energy_kwh,
            self._overnight_energy_kwh,
            len(self._frozen_solcast),
        )

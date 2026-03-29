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
    predict_day1_nighttime,
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
    BACKUP_MODE_ALWAYS,
    CONF_DEFAULT_DAILY,
    CONF_DEFAULT_OVERNIGHT,
    CONF_GUARD_THRESHOLD,
    CONF_FORECAST_DAYS,
    CONF_SOLCAST_REMAINING,
    CONF_GRID_POWER_ENTITY,
    CONF_TARGET_SOC,
    DEFAULT_TARGET_SOC,
    SOLCAST_STANDARD,
    SOLCAST_SHIFTED,
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

        # Internal consumption tracking
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

        # Frozen data for Days 2-7 overnight
        self._frozen_data: dict[int, DayResult] = {}

        # Current results
        self.results: dict[int, DayResult] = {}

        # Callback set with unregister support (fix #3)
        self._update_callbacks: set = set()

        # Persistent storage
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
        power_w = self.get_state_float(self.config[CONF_MAIN_POWER_ENTITY])
        power_kw = power_w / 1000.0
        overnight = self.is_overnight

        if self._last_power_reading is not None and self._last_power_time is not None:
            elapsed_hours = (now - self._last_power_time).total_seconds() / 3600
            if elapsed_hours > 0 and elapsed_hours < 1:
                avg_kw = (self._last_power_reading + power_kw) / 2
                energy_kwh = avg_kw * elapsed_hours
                self._daily_energy_kwh += energy_kwh
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

    def get_solcast_kwh(self, day: int) -> float:
        """Get Solcast forecast for a day, handling post-midnight shift."""
        now = dt_util.now()
        post_midnight = self.is_overnight and now.hour < 12

        mapping = SOLCAST_SHIFTED if post_midnight else SOLCAST_STANDARD

        conf_key = mapping.get(day)
        if conf_key is None:
            return 0.0

        entity_id = self.config.get(conf_key, "")
        return self.get_state_float(entity_id)

    def update(self) -> None:
        """Recalculate all predictions."""
        now = dt_util.now()
        sunrise, sunset = self._get_astral()

        if sunrise is None or sunset is None:
            return

        overnight = self.is_overnight
        target_kwh = self.target_soc / 100 * self.main.capacity_kwh
        consumption = self.get_consumption()

        overnight_params = get_overnight_params(
            sunrise=sunrise,
            sunset=sunset,
            overnight_kwh=consumption.avg_overnight_kwh,
            activation_hour=self.backup.activation_hour,
        )

        # Day 1
        main_soc_pct = self.get_state_float(self.config[CONF_MAIN_SOC_ENTITY])
        main_kwh = main_soc_pct / 100 * self.main.capacity_kwh

        if not overnight:
            remaining_solar = self.get_state_float(
                self.config.get(CONF_SOLCAST_REMAINING, "")
            )
            hours_to_sunset = max(0, (sunset - now).total_seconds() / 3600)
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
            )
        else:
            backup_available = 0.0
            backup_kw = 0.0
            if self.backup.enabled:
                backup_soc = self.get_state_float(self.config.get(CONF_BACKUP_SOC_ENTITY, ""))
                backup_available = max(
                    0,
                    (backup_soc - self.backup.floor_percent) / 100 * self.backup.capacity_kwh,
                )
                backup_kw = self.get_state_float(self.config.get(CONF_BACKUP_DISCHARGE_ENTITY, ""))

            hours_to_sunrise = max(0, (sunrise - now).total_seconds() / 3600)

            self.results[1] = predict_day1_nighttime(
                current_kwh=main_kwh,
                backup_available_kwh=backup_available,
                backup_kw=backup_kw,
                hours_to_sunrise=hours_to_sunrise,
                activation_hour=self.backup.activation_hour,
                now_dt=now,
                overnight_params=overnight_params,
                main=self.main,
            )

        # Days 2-N
        for day in range(2, self.num_days + 1):
            if overnight and day in self._frozen_data and self._frozen_data[day].soc_percent > 0:
                self.results[day] = self._frozen_data[day]
            else:
                prev_kwh = self.results.get(day - 1, DayResult(0, 0)).predicted_kwh
                solar = self.get_solcast_kwh(day)

                self.results[day] = predict_future_day(
                    prev_kwh=prev_kwh,
                    solar_kwh=solar,
                    consumption=consumption,
                    main=self.main,
                    backup=self.backup,
                    overnight_params=overnight_params,
                    target_kwh=target_kwh,
                )

        # Calculate grid needed for each day
        for day in range(1, self.num_days + 1):
            if day in self.results:
                result = self.results[day]
                deficit = target_kwh - result.predicted_kwh
                result.grid_needed_kwh = round(max(0, deficit), 2)

        # Notify sensors
        for cb in self._update_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.debug("Error in update callback", exc_info=True)

    def freeze(self) -> None:
        """Freeze Days 2-7 values for overnight."""
        for day in range(2, self.num_days + 1):
            if day in self.results:
                self._frozen_data[day] = self.results[day]
        _LOGGER.debug("Frozen SoC data captured for days 2-%d", self.num_days)

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
                "daily_energy_kwh": self._daily_energy_kwh,
                "overnight_energy_kwh": self._overnight_energy_kwh,
                "grid_energy_today_kwh": self._grid_energy_today_kwh,
                "frozen_data": {
                    str(k): {
                        "soc_percent": v.soc_percent,
                        "predicted_kwh": v.predicted_kwh,
                        "solcast_kwh": v.solcast_kwh,
                        "daytime_consumption_kwh": v.daytime_consumption_kwh,
                        "backup_charged_kwh": v.backup_charged_kwh,
                        "grid_needed_kwh": v.grid_needed_kwh,
                    }
                    for k, v in self._frozen_data.items()
                },
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

        # Only restore frozen data if storage version matches
        stored_version = data.get("storage_version", 0)
        if stored_version != STORAGE_VERSION:
            _LOGGER.info(
                "Storage version changed (%s → %s), discarding stale frozen data",
                stored_version,
                STORAGE_VERSION,
            )
        else:
            frozen = data.get("frozen_data", {})
            for k, v in frozen.items():
                try:
                    self._frozen_data[int(k)] = DayResult(
                        soc_percent=v.get("soc_percent", 0),
                        predicted_kwh=v.get("predicted_kwh", 0),
                        solcast_kwh=v.get("solcast_kwh", 0),
                        daytime_consumption_kwh=v.get("daytime_consumption_kwh", 0),
                        backup_charged_kwh=v.get("backup_charged_kwh", 0),
                        grid_needed_kwh=v.get("grid_needed_kwh", 0),
                    )
                except (TypeError, ValueError):
                    _LOGGER.warning("Skipping corrupt frozen data for day %s", k)

        _LOGGER.info(
            "State restored: %d daily, %d overnight history entries, "
            "%.1f kWh daily accum, %.1f kWh overnight accum, %d frozen days",
            len(self._daily_history),
            len(self._overnight_history),
            self._daily_energy_kwh,
            self._overnight_energy_kwh,
            len(self._frozen_data),
        )

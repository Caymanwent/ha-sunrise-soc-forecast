"""Sensor platform for Sunrise SoC Forecast."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    config = {**entry.data, **entry.options}
    num_days = config.get(CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS)

    entities = []

    # Day 1-N forecast sensors
    for day in range(1, num_days + 1):
        entities.append(SunriseSocSensor(coordinator, day, entry))

    # Consumption average sensors
    entities.append(ConsumptionAverageSensor(coordinator, "daily", entry))
    entities.append(ConsumptionAverageSensor(coordinator, "overnight", entry))

    # Next predicted moment both batteries are full
    entities.append(BatteriesFullSensor(coordinator, entry))

    async_add_entities(entities)


class SunriseSocSensor(SensorEntity):
    """Sensor for predicted sunrise SoC."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:battery-clock"

    def __init__(
        self,
        coordinator,
        day: int,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        self._coordinator = coordinator
        self._day = day
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sunrise_soc_day_{day}"
        self._attr_name = f"Predicted Sunrise SoC Day {day}"

    async def async_added_to_hass(self) -> None:
        """Register update callback."""
        self._coordinator.register_callback(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister update callback."""
        self._coordinator.unregister_callback(self._handle_update)

    @callback
    def _handle_update(self) -> None:
        """Handle coordinator update."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the SoC percentage."""
        result = self._coordinator.results.get(self._day)
        if result is None:
            return None
        return result.soc_percent

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        result = self._coordinator.results.get(self._day)
        if result is None:
            return {}

        attrs = {
            "predicted_kwh": result.predicted_kwh,
            "daytime_consumption_kwh": result.daytime_consumption_kwh,
            "backup_charged_kwh": result.backup_charged_kwh,
            "grid_needed_kwh": result.grid_needed_kwh,
            "grid_used_today_kwh": self._coordinator.grid_energy_today,
            "target_soc": self._coordinator.target_soc,
        }

        if self._day == 1:
            attrs["mode"] = "nighttime" if self._coordinator.is_overnight else "daytime"
            attrs["remaining_solar_kwh"] = result.solcast_kwh if not self._coordinator.is_overnight else 0
            attrs["using_fallback"] = self._coordinator.get_consumption().using_fallback
        else:
            attrs["solcast_kwh"] = result.solcast_kwh
            attrs["morning_low_kwh"] = result.morning_low_kwh
            attrs["morning_low_pct"] = result.morning_low_pct
            if self._day == 2 and (
                self._coordinator.shed_threshold_pct > 0 or self._coordinator.shed_entities
            ):
                # load-shedding diagnostics live on the Day-2 sensor (the one the automations key on)
                attrs["shed_active"] = self._coordinator.shed_active
                attrs["shed_hours_skipped"] = self._coordinator.shed_hours_skipped
            if result.floor_hour:
                attrs["floor_hour"] = result.floor_hour
            if result.night_floor_hour:
                attrs["night_floor_hour"] = result.night_floor_hour

        # Full-battery prediction: booleans always, HH:MM only when predicted
        attrs["main_full"] = result.main_full
        attrs["backup_full"] = result.backup_full
        if result.main_full_hour is not None:
            attrs["main_full_hour"] = result.main_full_hour
        if result.backup_full_hour is not None:
            attrs["backup_full_hour"] = result.backup_full_hour
        if result.both_full_hour is not None:
            attrs["both_full_hour"] = result.both_full_hour

        return attrs


class BatteriesFullSensor(SensorEntity):
    """Next predicted moment at which both the main and the backup battery are full.

    Searches Day 1..N in order: Day 1 simulates from now, so if it predicts no
    full charge today (cloudy, or it is already evening) the state falls through
    to Day 2 = tomorrow, and so on. Unknown when no forecast day gets there.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:battery-charging-100"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_batteries_full"
        self._attr_name = "Predicted Batteries Full"

    async def async_added_to_hass(self) -> None:
        """Register update callback."""
        self._coordinator.register_callback(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister update callback."""
        self._coordinator.unregister_callback(self._handle_update)

    @callback
    def _handle_update(self) -> None:
        """Handle coordinator update."""
        self.async_write_ha_state()

    def _next_full(self) -> tuple[int, Any, datetime] | None:
        """First forecast day (1..N) that predicts both batteries full → (day, result, when)."""
        for day in range(1, self._coordinator.num_days + 1):
            result = self._coordinator.results.get(day)
            if result is None or result.both_full_time is None:
                continue
            when = self._coordinator.full_datetime(day, result.both_full_time)
            if when is not None:
                return day, result, when
        return None

    @property
    def native_value(self) -> datetime | None:
        """Return the next predicted both-full datetime."""
        found = self._next_full()
        return found[2] if found else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return which day it is, the per-battery times and a readable summary."""
        found = self._next_full()
        if found is None:
            return {
                "detail": (
                    f"No day in the {self._coordinator.num_days}-day forecast "
                    "predicts both batteries full"
                ),
            }
        day, result, when = found
        coordinator = self._coordinator
        day_date = coordinator.day_start(day).date().isoformat()
        main_at = coordinator.full_datetime(day, result.main_full_time)
        backup_at = coordinator.full_datetime(day, result.backup_full_time)
        if coordinator.backup.enabled:
            detail = (
                f"Day {day} ({day_date}): main full {result.main_full_hour}, "
                f"backup full {result.backup_full_hour}"
            )
        else:
            detail = (
                f"Day {day} ({day_date}): main full {result.main_full_hour} "
                "(no backup battery configured)"
            )
        if when <= dt_util.now():
            # Day 1 with the live batteries full: the coordinator latches the
            # moment they got there, so the state is in the past, not creeping
            detail += " — full now"
        return {
            "day": day,
            "date": day_date,
            "main_full_at": main_at.isoformat() if main_at else None,
            "backup_full_at": backup_at.isoformat() if backup_at else None,
            "detail": detail,
        }


class ConsumptionAverageSensor(SensorEntity):
    """Sensor for 7-day consumption average."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "kWh"

    def __init__(
        self,
        coordinator,
        period: str,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        self._coordinator = coordinator
        self._period = period
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_7day_avg_{period}"
        self._attr_name = f"7 Day Average {'Daily' if period == 'daily' else 'Overnight'} Consumption"

    async def async_added_to_hass(self) -> None:
        """Register update callback."""
        self._coordinator.register_callback(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister update callback."""
        self._coordinator.unregister_callback(self._handle_update)

    @callback
    def _handle_update(self) -> None:
        """Handle coordinator update."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the average."""
        if self._period == "daily":
            return self._coordinator.daily_average
        return self._coordinator.overnight_average

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return history and hourly breakdown."""
        if self._period == "daily":
            history = self._coordinator.daily_history
        else:
            history = self._coordinator.overnight_history

        attrs = {"history": history, "days": len(history)}

        # Add hourly averages
        hourly = self._coordinator.get_hourly_averages()
        attrs["hourly_averages"] = hourly
        attrs["hourly_detail"] = {
            f"{h:02d}:00": round(hourly[h], 2) for h in range(24)
        }
        attrs["hourly_history"] = {
            f"{h:02d}:00": ", ".join(f"{v:05.2f}" for v in self._coordinator._hourly_history[h])
            for h in range(24)
            if self._coordinator._hourly_history[h]
        }

        return attrs

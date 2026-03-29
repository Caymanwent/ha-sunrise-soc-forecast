"""Config flow for Sunrise SoC Forecast integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
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
    CONF_DEFAULT_DAILY,
    CONF_DEFAULT_OVERNIGHT,
    CONF_GUARD_THRESHOLD,
    CONF_SOLCAST_REMAINING,
    CONF_SOLCAST_TOMORROW,
    CONF_SOLCAST_DAY_3,
    CONF_SOLCAST_DAY_4,
    CONF_SOLCAST_DAY_5,
    CONF_SOLCAST_DAY_6,
    CONF_SOLCAST_DAY_7,
    CONF_GRID_POWER_ENTITY,
    CONF_BACKUP_MODE,
    BACKUP_MODE_ALWAYS,
    BACKUP_MODE_TARGET,
    CONF_FORECAST_DAYS,
    CONF_TARGET_SOC,
    DEFAULT_MAIN_CAPACITY,
    DEFAULT_MAIN_FLOOR,
    DEFAULT_BACKUP_CAPACITY,
    DEFAULT_BACKUP_FLOOR,
    DEFAULT_BACKUP_DISCHARGE_KW,
    DEFAULT_BACKUP_ACTIVATION_HOUR,
    DEFAULT_DAILY_CONSUMPTION,
    DEFAULT_OVERNIGHT_CONSUMPTION,
    DEFAULT_GUARD_THRESHOLD,
    DEFAULT_FORECAST_DAYS,
    DEFAULT_TARGET_SOC,
)

ENTITY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor")
)


class SunriseSocForecastConfigFlow(
    config_entries.ConfigFlow, domain=DOMAIN
):
    """Handle a config flow for Sunrise SoC Forecast."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._data: dict = {}

    async def async_step_user(self, user_input=None):
        """Step 1: Main battery configuration."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_backup()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAIN_SOC_ENTITY): ENTITY_SELECTOR,
                    vol.Required(CONF_MAIN_POWER_ENTITY): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_MAIN_CAPACITY, default=DEFAULT_MAIN_CAPACITY
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.1)),
                    vol.Required(
                        CONF_MAIN_FLOOR, default=DEFAULT_MAIN_FLOOR
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                    vol.Optional(CONF_GRID_POWER_ENTITY): ENTITY_SELECTOR,
                }
            ),
        )

    async def async_step_backup(self, user_input=None):
        """Step 2: Backup battery toggle."""
        if user_input is not None:
            self._data.update(user_input)
            if user_input.get(CONF_BACKUP_ENABLED, False):
                return await self.async_step_backup_details()
            return await self.async_step_solcast()

        return self.async_show_form(
            step_id="backup",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BACKUP_ENABLED, default=False): bool,
                }
            ),
        )

    async def async_step_backup_details(self, user_input=None):
        """Step 2b: Backup battery details."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_solcast()

        return self.async_show_form(
            step_id="backup_details",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BACKUP_SOC_ENTITY): ENTITY_SELECTOR,
                    vol.Required(CONF_BACKUP_DISCHARGE_ENTITY): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_BACKUP_CAPACITY, default=DEFAULT_BACKUP_CAPACITY
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.1)),
                    vol.Required(
                        CONF_BACKUP_FLOOR, default=DEFAULT_BACKUP_FLOOR
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                    vol.Required(
                        CONF_BACKUP_DISCHARGE_KW, default=DEFAULT_BACKUP_DISCHARGE_KW
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.1)),
                    vol.Required(
                        CONF_BACKUP_ACTIVATION_HOUR,
                        default=DEFAULT_BACKUP_ACTIVATION_HOUR,
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=6)),
                    vol.Required(
                        CONF_BACKUP_MODE, default=BACKUP_MODE_ALWAYS
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=BACKUP_MODE_ALWAYS, label="Always discharge nightly"),
                                selector.SelectOptionDict(value=BACKUP_MODE_TARGET, label="Only if needed to reach target SoC"),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_solcast(self, user_input=None):
        """Step 3: Solcast entity configuration."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_options()

        return self.async_show_form(
            step_id="solcast",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SOLCAST_REMAINING): ENTITY_SELECTOR,
                    vol.Required(CONF_SOLCAST_TOMORROW): ENTITY_SELECTOR,
                    vol.Required(CONF_SOLCAST_DAY_3): ENTITY_SELECTOR,
                    vol.Required(CONF_SOLCAST_DAY_4): ENTITY_SELECTOR,
                    vol.Required(CONF_SOLCAST_DAY_5): ENTITY_SELECTOR,
                    vol.Required(CONF_SOLCAST_DAY_6): ENTITY_SELECTOR,
                    vol.Required(CONF_SOLCAST_DAY_7): ENTITY_SELECTOR,
                }
            ),
        )

    async def async_step_options(self, user_input=None):
        """Step 4: Forecast options and consumption defaults."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="Sunrise SoC Forecast",
                data=self._data,
            )

        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FORECAST_DAYS, default=DEFAULT_FORECAST_DAYS
                    ): vol.All(vol.Coerce(int), vol.Range(min=2, max=7)),
                    vol.Required(
                        CONF_TARGET_SOC, default=DEFAULT_TARGET_SOC
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                    vol.Required(
                        CONF_DEFAULT_DAILY, default=DEFAULT_DAILY_CONSUMPTION
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_DEFAULT_OVERNIGHT, default=DEFAULT_OVERNIGHT_CONSUMPTION
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_GUARD_THRESHOLD, default=DEFAULT_GUARD_THRESHOLD
                    ): vol.Coerce(float),
                }
            ),
        )

    @classmethod
    @callback
    def async_get_options_flow(cls, config_entry):
        """Get the options flow."""
        return SunriseSocOptionsFlow()


class SunriseSocOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow — full reconfiguration of all settings."""

    def _get_data(self) -> dict:
        """Get merged data + options with current values."""
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_init(self, user_input=None):
        """Step 1: Main battery."""
        if user_input is not None:
            self._data = user_input
            return await self.async_step_backup()

        data = self._get_data()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MAIN_SOC_ENTITY,
                        default=data.get(CONF_MAIN_SOC_ENTITY),
                    ): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_MAIN_POWER_ENTITY,
                        default=data.get(CONF_MAIN_POWER_ENTITY),
                    ): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_MAIN_CAPACITY,
                        default=data.get(CONF_MAIN_CAPACITY, DEFAULT_MAIN_CAPACITY),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.1)),
                    vol.Required(
                        CONF_MAIN_FLOOR,
                        default=data.get(CONF_MAIN_FLOOR, DEFAULT_MAIN_FLOOR),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                    vol.Optional(
                        CONF_GRID_POWER_ENTITY,
                    ): ENTITY_SELECTOR,
                }
            ),
        )

    async def async_step_backup(self, user_input=None):
        """Step 2: Backup battery toggle."""
        if user_input is not None:
            self._data.update(user_input)
            if user_input.get(CONF_BACKUP_ENABLED, False):
                return await self.async_step_backup_details()
            return await self.async_step_solcast()

        data = self._get_data()

        return self.async_show_form(
            step_id="backup",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BACKUP_ENABLED,
                        default=data.get(CONF_BACKUP_ENABLED, False),
                    ): bool,
                }
            ),
        )

    async def async_step_backup_details(self, user_input=None):
        """Step 2b: Backup battery details."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_solcast()

        data = self._get_data()

        return self.async_show_form(
            step_id="backup_details",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BACKUP_SOC_ENTITY,
                        default=data.get(CONF_BACKUP_SOC_ENTITY),
                    ): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_BACKUP_DISCHARGE_ENTITY,
                        default=data.get(CONF_BACKUP_DISCHARGE_ENTITY),
                    ): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_BACKUP_CAPACITY,
                        default=data.get(CONF_BACKUP_CAPACITY, DEFAULT_BACKUP_CAPACITY),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.1)),
                    vol.Required(
                        CONF_BACKUP_FLOOR,
                        default=data.get(CONF_BACKUP_FLOOR, DEFAULT_BACKUP_FLOOR),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                    vol.Required(
                        CONF_BACKUP_DISCHARGE_KW,
                        default=data.get(CONF_BACKUP_DISCHARGE_KW, DEFAULT_BACKUP_DISCHARGE_KW),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.1)),
                    vol.Required(
                        CONF_BACKUP_ACTIVATION_HOUR,
                        default=data.get(CONF_BACKUP_ACTIVATION_HOUR, DEFAULT_BACKUP_ACTIVATION_HOUR),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=6)),
                    vol.Required(
                        CONF_BACKUP_MODE,
                        default=data.get(CONF_BACKUP_MODE, BACKUP_MODE_ALWAYS),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=BACKUP_MODE_ALWAYS, label="Always discharge nightly"),
                                selector.SelectOptionDict(value=BACKUP_MODE_TARGET, label="Only if needed to reach target SoC"),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_solcast(self, user_input=None):
        """Step 3: Solcast entities."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_options()

        data = self._get_data()

        return self.async_show_form(
            step_id="solcast",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SOLCAST_REMAINING,
                        default=data.get(CONF_SOLCAST_REMAINING),
                    ): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_SOLCAST_TOMORROW,
                        default=data.get(CONF_SOLCAST_TOMORROW),
                    ): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_SOLCAST_DAY_3,
                        default=data.get(CONF_SOLCAST_DAY_3),
                    ): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_SOLCAST_DAY_4,
                        default=data.get(CONF_SOLCAST_DAY_4),
                    ): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_SOLCAST_DAY_5,
                        default=data.get(CONF_SOLCAST_DAY_5),
                    ): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_SOLCAST_DAY_6,
                        default=data.get(CONF_SOLCAST_DAY_6),
                    ): ENTITY_SELECTOR,
                    vol.Required(
                        CONF_SOLCAST_DAY_7,
                        default=data.get(CONF_SOLCAST_DAY_7),
                    ): ENTITY_SELECTOR,
                }
            ),
        )

    async def async_step_options(self, user_input=None):
        """Step 4: Forecast options."""
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="", data=self._data)

        data = self._get_data()

        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FORECAST_DAYS,
                        default=data.get(CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS),
                    ): vol.All(vol.Coerce(int), vol.Range(min=2, max=7)),
                    vol.Required(
                        CONF_TARGET_SOC,
                        default=data.get(CONF_TARGET_SOC, DEFAULT_TARGET_SOC),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
                    vol.Required(
                        CONF_DEFAULT_DAILY,
                        default=data.get(CONF_DEFAULT_DAILY, DEFAULT_DAILY_CONSUMPTION),
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_DEFAULT_OVERNIGHT,
                        default=data.get(CONF_DEFAULT_OVERNIGHT, DEFAULT_OVERNIGHT_CONSUMPTION),
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_GUARD_THRESHOLD,
                        default=data.get(CONF_GUARD_THRESHOLD, DEFAULT_GUARD_THRESHOLD),
                    ): vol.Coerce(float),
                }
            ),
        )

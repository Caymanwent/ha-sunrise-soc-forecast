"""Constants for Sunrise SoC Forecast integration."""

DOMAIN = "sunrise_soc_forecast"

# Increment this when calculation logic changes to invalidate stale frozen data
STORAGE_VERSION = 4

# Config keys
CONF_MAIN_SOC_ENTITY = "main_soc_entity"
CONF_MAIN_POWER_ENTITY = "main_power_entity"
CONF_MAIN_CAPACITY = "main_capacity"
CONF_MAIN_FLOOR = "main_floor"

CONF_BACKUP_ENABLED = "backup_enabled"
CONF_BACKUP_SOC_ENTITY = "backup_soc_entity"
CONF_BACKUP_DISCHARGE_ENTITY = "backup_discharge_entity"
CONF_BACKUP_CAPACITY = "backup_capacity"
CONF_BACKUP_FLOOR = "backup_floor"
CONF_BACKUP_DISCHARGE_KW = "backup_discharge_kw"
CONF_BACKUP_ACTIVATION_HOUR = "backup_activation_hour"
CONF_BACKUP_MODE = "backup_mode"

# Backup deployment modes
BACKUP_MODE_ALWAYS = "always"
BACKUP_MODE_TARGET = "target_based"

CONF_DEFAULT_DAILY = "default_daily"
CONF_DEFAULT_OVERNIGHT = "default_overnight"
CONF_GUARD_THRESHOLD = "guard_threshold"

CONF_SOLCAST_REMAINING = "solcast_remaining_today"
CONF_SOLCAST_FORECAST_TODAY = "solcast_forecast_today"
CONF_SOLCAST_TOMORROW = "solcast_tomorrow"
CONF_SOLCAST_DAY_3 = "solcast_day_3"
CONF_SOLCAST_DAY_4 = "solcast_day_4"
CONF_SOLCAST_DAY_5 = "solcast_day_5"
CONF_SOLCAST_DAY_6 = "solcast_day_6"
CONF_SOLCAST_DAY_7 = "solcast_day_7"

CONF_GRID_POWER_ENTITY = "grid_power_entity"

CONF_FORECAST_DAYS = "forecast_days"
CONF_TARGET_SOC = "target_soc"

# Defaults
DEFAULT_MAIN_CAPACITY = 128.0
DEFAULT_MAIN_FLOOR = 5.0
DEFAULT_BACKUP_CAPACITY = 16.0
DEFAULT_BACKUP_FLOOR = 5.0
DEFAULT_BACKUP_DISCHARGE_KW = 6.0
DEFAULT_BACKUP_ACTIVATION_HOUR = 2
DEFAULT_DAILY_CONSUMPTION = 128.0
DEFAULT_OVERNIGHT_CONSUMPTION = 64.0
DEFAULT_GUARD_THRESHOLD = 3.0
DEFAULT_FORECAST_DAYS = 7
DEFAULT_TARGET_SOC = 20.0

# Solcast standard mapping: day -> config key
SOLCAST_STANDARD = {
    2: CONF_SOLCAST_TOMORROW,
    3: CONF_SOLCAST_DAY_3,
    4: CONF_SOLCAST_DAY_4,
    5: CONF_SOLCAST_DAY_5,
    6: CONF_SOLCAST_DAY_6,
    7: CONF_SOLCAST_DAY_7,
}

# Solcast shifted mapping (post-midnight): day -> config key
SOLCAST_SHIFTED = {
    2: CONF_SOLCAST_REMAINING,
    3: CONF_SOLCAST_TOMORROW,
    4: CONF_SOLCAST_DAY_3,
    5: CONF_SOLCAST_DAY_4,
    6: CONF_SOLCAST_DAY_5,
    7: CONF_SOLCAST_DAY_6,
}

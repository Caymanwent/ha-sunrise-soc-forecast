"""SoC calculation engine for Sunrise SoC Forecast."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class BatteryConfig:
    """Battery configuration."""

    capacity_kwh: float
    floor_percent: float

    @property
    def floor_kwh(self) -> float:
        return self.capacity_kwh * self.floor_percent / 100

    @property
    def usable_kwh(self) -> float:
        return self.capacity_kwh * (100 - self.floor_percent) / 100


@dataclass
class BackupConfig:
    """Backup battery configuration."""

    enabled: bool
    capacity_kwh: float = 0.0
    floor_percent: float = 0.0
    fixed_discharge_kw: float = 0.0
    activation_hour: int = 2
    mode: str = "always"  # "always" or "target_based"

    @property
    def usable_kwh(self) -> float:
        if not self.enabled:
            return 0.0
        return self.capacity_kwh * (100 - self.floor_percent) / 100


@dataclass
class ConsumptionData:
    """Consumption averages and fallbacks."""

    avg_daily_kwh: float
    avg_overnight_kwh: float
    using_fallback: bool


@dataclass
class OvernightParams:
    """Overnight calculation parameters."""

    overnight_hours: float
    avg_overnight_kw: float
    hours_sunset_to_activation: float
    hours_activation_to_sunrise: float


@dataclass
class DayResult:
    """Result of a day's SoC prediction."""

    soc_percent: float
    predicted_kwh: float
    solcast_kwh: float = 0.0
    daytime_consumption_kwh: float = 0.0
    backup_charged_kwh: float = 0.0
    grid_needed_kwh: float = 0.0


def get_consumption(
    daily_avg: float | None,
    overnight_avg: float | None,
    default_daily: float,
    default_overnight: float,
    guard_threshold: float,
) -> ConsumptionData:
    """Get consumption data with fallback logic."""
    daily_valid = daily_avg is not None and daily_avg > guard_threshold
    overnight_valid = overnight_avg is not None and overnight_avg > guard_threshold
    both_valid = daily_valid and overnight_valid

    if both_valid:
        return ConsumptionData(
            avg_daily_kwh=daily_avg,
            avg_overnight_kwh=overnight_avg,
            using_fallback=False,
        )
    return ConsumptionData(
        avg_daily_kwh=default_daily,
        avg_overnight_kwh=default_overnight,
        using_fallback=True,
    )


def get_overnight_params(
    sunrise: datetime,
    sunset: datetime,
    overnight_kwh: float,
    activation_hour: int,
) -> OvernightParams:
    """Calculate overnight timing parameters."""
    overnight_hours = abs((sunrise - sunset).total_seconds() / 3600)
    if overnight_hours <= 0:
        overnight_hours = 12.0

    avg_overnight_kw = overnight_kwh / overnight_hours

    # Phase split: sunset → activation hour, activation → sunrise
    sunset_local = sunset.astimezone()
    sunset_hour = sunset_local.hour + sunset_local.minute / 60
    hours_to_activation = (24 - sunset_hour) + activation_hour
    if hours_to_activation > overnight_hours:
        hours_to_activation = overnight_hours
    hours_after_activation = overnight_hours - hours_to_activation
    if hours_after_activation < 0:
        hours_after_activation = 0.0

    return OvernightParams(
        overnight_hours=overnight_hours,
        avg_overnight_kw=avg_overnight_kw,
        hours_sunset_to_activation=hours_to_activation,
        hours_activation_to_sunrise=hours_after_activation,
    )


def calc_overnight_drain_no_backup(
    sunset_kwh: float,
    params: OvernightParams,
    main_floor_kwh: float,
    main_cap: float,
) -> float:
    """Calculate sunrise kWh with NO backup assist."""
    main_kwh = sunset_kwh - (params.avg_overnight_kw * params.overnight_hours)
    return max(main_floor_kwh, min(main_cap, main_kwh))


def calc_overnight_drain_with_backup(
    sunset_kwh: float,
    backup_charged_kwh: float,
    backup_kw: float,
    params: OvernightParams,
    main_floor_kwh: float,
    main_cap: float,
) -> float:
    """Calculate sunrise kWh with backup assist (always mode)."""
    avg_kw = params.avg_overnight_kw

    # Phase 1: sunset → backup activation, main covers all load
    main_kwh = sunset_kwh - (avg_kw * params.hours_sunset_to_activation)

    # Phase 2: activation → sunrise, backup assists
    h2 = params.hours_activation_to_sunrise
    if backup_charged_kwh > 0 and backup_kw > 0 and h2 > 0:
        backup_hours = backup_charged_kwh / backup_kw
        net_kw = backup_kw - avg_kw
        if backup_hours >= h2:
            main_kwh += net_kw * h2
        else:
            main_kwh += net_kw * backup_hours
            remaining = h2 - backup_hours
            main_kwh -= avg_kw * remaining
    else:
        main_kwh -= avg_kw * h2

    return max(main_floor_kwh, min(main_cap, main_kwh))


def calc_overnight_drain(
    sunset_kwh: float,
    backup_charged_kwh: float,
    backup_kw: float,
    params: OvernightParams,
    main_floor_kwh: float,
    main_cap: float,
    backup_mode: str = "always",
    target_kwh: float = 0.0,
) -> float:
    """Calculate main battery kWh at sunrise after overnight drain.

    backup_mode:
        "always" — backup activates every night at the set hour
        "target_based" — backup only activates if main would drop below target
                         and only discharges enough to reach target
    """
    if backup_mode == "always":
        return calc_overnight_drain_with_backup(
            sunset_kwh, backup_charged_kwh, backup_kw,
            params, main_floor_kwh, main_cap,
        )

    # Target-based mode: first calculate without backup
    sunrise_no_backup = calc_overnight_drain_no_backup(
        sunset_kwh, params, main_floor_kwh, main_cap,
    )

    if sunrise_no_backup >= target_kwh or target_kwh <= 0:
        # Main can handle it alone — backup stays idle
        return sunrise_no_backup

    # Main falls short — calculate with full backup
    sunrise_with_backup = calc_overnight_drain_with_backup(
        sunset_kwh, backup_charged_kwh, backup_kw,
        params, main_floor_kwh, main_cap,
    )

    # Cap at target — don't use more backup than needed
    if sunrise_with_backup > target_kwh:
        return target_kwh

    # Even with full backup, can't reach target
    return sunrise_with_backup


def predict_day1_daytime(
    current_kwh: float,
    remaining_solar: float,
    hours_to_sunset: float,
    total_daytime_hours: float,
    consumption: ConsumptionData,
    main: BatteryConfig,
    backup: BackupConfig,
    backup_soc_pct: float,
    overnight_params: OvernightParams,
    target_kwh: float = 0.0,
) -> DayResult:
    """Day 1 daytime prediction: current SoC + remaining solar → sunset → overnight."""
    daytime_kwh = max(0, consumption.avg_daily_kwh - consumption.avg_overnight_kwh)

    if total_daytime_hours > 0:
        remaining_daytime = daytime_kwh * (hours_to_sunset / total_daytime_hours)
    else:
        remaining_daytime = 0.0

    net_remaining = remaining_solar - remaining_daytime
    sunset_kwh = max(main.floor_kwh, min(main.capacity_kwh, current_kwh + net_remaining))

    # Backup charge from solar surplus + current backup charge
    solar_for_backup = net_remaining - (main.capacity_kwh - current_kwh)
    solar_for_backup = max(0, solar_for_backup)

    backup_current = 0.0
    if backup.enabled:
        backup_current = max(0, (backup_soc_pct - backup.floor_percent) / 100 * backup.capacity_kwh)

    backup_charged = min(backup.usable_kwh, backup_current + solar_for_backup) if backup.enabled else 0.0

    sunrise_kwh = calc_overnight_drain(
        sunset_kwh=sunset_kwh,
        backup_charged_kwh=backup_charged,
        backup_kw=backup.fixed_discharge_kw if backup.enabled else 0,
        params=overnight_params,
        main_floor_kwh=main.floor_kwh,
        main_cap=main.capacity_kwh,
        backup_mode=backup.mode if backup.enabled else "always",
        target_kwh=target_kwh,
    )

    return DayResult(
        soc_percent=round(sunrise_kwh / main.capacity_kwh * 100, 1),
        predicted_kwh=round(sunrise_kwh, 2),
        solcast_kwh=round(remaining_solar, 2),
        daytime_consumption_kwh=round(remaining_daytime, 2),
        backup_charged_kwh=round(backup_charged, 2),
    )


def predict_day1_nighttime(
    # Note: backup_mode is intentionally not applied here.
    # At night, we use live sensor readings (actual SoC + actual backup state)
    # rather than the target-based logic which is for future day planning.
    current_kwh: float,
    backup_available_kwh: float,
    backup_kw: float,
    hours_to_sunrise: float,
    activation_hour: int,
    now_dt: datetime,
    overnight_params: OvernightParams,
    main: BatteryConfig,
) -> DayResult:
    """Day 1 nighttime prediction: current SoC - average drain + live backup."""
    avg_kw = overnight_params.avg_overnight_kw

    # Calculate phase split from now
    two_am = now_dt.replace(hour=activation_hour, minute=0, second=0, microsecond=0)
    if now_dt >= two_am:
        two_am += timedelta(days=1)

    if now_dt.hour >= activation_hour and now_dt.hour < 12:
        hours_phase1 = 0.0
        hours_phase2 = hours_to_sunrise
    else:
        hours_phase1 = max(0, (two_am - now_dt).total_seconds() / 3600)
        hours_phase1 = min(hours_phase1, hours_to_sunrise)
        hours_phase2 = hours_to_sunrise - hours_phase1

    main_kwh = current_kwh

    # Phase 1: now → activation, main covers all
    if hours_phase1 > 0:
        main_kwh -= avg_kw * hours_phase1

    # Phase 2: activation → sunrise, backup assists
    if hours_phase2 > 0:
        if backup_available_kwh <= 0 or backup_kw <= 0:
            main_kwh -= avg_kw * hours_phase2
        else:
            backup_hours = backup_available_kwh / backup_kw
            net_kw = backup_kw - avg_kw
            if backup_hours >= hours_phase2:
                main_kwh += net_kw * hours_phase2
            else:
                main_kwh += net_kw * backup_hours
                remaining = hours_phase2 - backup_hours
                main_kwh -= avg_kw * remaining

    main_kwh = max(main.floor_kwh, min(main.capacity_kwh, main_kwh))

    return DayResult(
        soc_percent=round(main_kwh / main.capacity_kwh * 100, 1),
        predicted_kwh=round(main_kwh, 2),
    )


def predict_future_day(
    prev_kwh: float,
    solar_kwh: float,
    consumption: ConsumptionData,
    main: BatteryConfig,
    backup: BackupConfig,
    overnight_params: OvernightParams,
    target_kwh: float = 0.0,
) -> DayResult:
    """Predict SoC for Days 2-7."""
    daytime_kwh = max(0, consumption.avg_daily_kwh - consumption.avg_overnight_kwh)
    net_solar = solar_kwh - daytime_kwh

    # Sunset SoC
    sunset_kwh = max(main.floor_kwh, min(main.capacity_kwh, prev_kwh + net_solar))

    # Backup charge from solar surplus
    solar_for_backup = max(0, net_solar - (main.capacity_kwh - prev_kwh))
    backup_charged = min(backup.usable_kwh, solar_for_backup) if backup.enabled else 0.0

    sunrise_kwh = calc_overnight_drain(
        sunset_kwh=sunset_kwh,
        backup_charged_kwh=backup_charged,
        backup_kw=backup.fixed_discharge_kw if backup.enabled else 0,
        params=overnight_params,
        main_floor_kwh=main.floor_kwh,
        main_cap=main.capacity_kwh,
        backup_mode=backup.mode if backup.enabled else "always",
        target_kwh=target_kwh,
    )

    return DayResult(
        soc_percent=round(sunrise_kwh / main.capacity_kwh * 100, 1),
        predicted_kwh=round(sunrise_kwh, 2),
        solcast_kwh=round(solar_kwh, 2),
        daytime_consumption_kwh=round(daytime_kwh, 2),
        backup_charged_kwh=round(backup_charged, 2),
    )

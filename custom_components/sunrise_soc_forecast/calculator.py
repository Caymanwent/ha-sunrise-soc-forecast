"""SoC calculation engine for Sunrise SoC Forecast."""

from __future__ import annotations

import math
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
    morning_low_kwh: float = 0.0
    morning_low_pct: float = 0.0


@dataclass
class DaytimeResult:
    """Result of daytime simulation."""

    sunset_kwh: float
    surplus_kwh: float
    morning_low_kwh: float
    total_consumption_kwh: float = 0.0


def simulate_daytime(
    start_kwh: float,
    solar_total_kwh: float,
    daytime_consumption_kwh: float,
    daytime_hours: float,
    battery_cap: float,
    battery_floor: float,
    hourly_consumption: list[float] | None = None,
    sunrise_hour: float = 6.0,
    hourly_solar: list[float] | None = None,
    inverter_efficiency: float = 1.0,
    sunset_hour: float = 18.0,
) -> DaytimeResult:
    """Simulate hour-by-hour daytime using a sine curve for solar production.

    If hourly_consumption is provided (24 floats, kWh per hour), uses actual
    hourly rates for the daytime hours. Otherwise falls back to flat rate.

    Returns DaytimeResult with sunset_kwh, surplus energy, and morning low.
    morning_low_kwh = lowest battery level before solar exceeds consumption.
    """
    # Calculate steps from actual hour span (fix #3)
    if hourly_consumption and len(hourly_consumption) == 24:
        steps = max(1, int(math.ceil(sunset_hour)) - int(math.floor(sunrise_hour)))
    else:
        steps = max(1, int(round(daytime_hours)))

    # Build per-step consumption from hourly data or flat rate
    if hourly_consumption and len(hourly_consumption) == 24:
        consumption_per_step = []
        sr_int = int(math.floor(sunrise_hour))
        for i in range(steps):
            hour_idx = (sr_int + i) % 24
            if i == 0:
                frac = 1.0 - (sunrise_hour - math.floor(sunrise_hour))
            elif i == steps - 1:
                frac = sunset_hour - math.floor(sunset_hour)
                if frac <= 0:
                    frac = 1.0
            else:
                frac = 1.0
            consumption_per_step.append(hourly_consumption[hour_idx] * frac)
    else:
        flat_rate = daytime_consumption_kwh / steps if steps > 0 else 0
        consumption_per_step = [flat_rate] * steps

    # Solar production per step: use Solcast data if available, else sine curve
    if hourly_solar and len(hourly_solar) == 48:
        # Half-hourly Solcast forecast: 48 entries, run 2 steps per hour
        sr_half = int(math.floor(sunrise_hour * 2))
        ss_half = int(math.ceil(sunset_hour * 2))
        half_steps = max(1, ss_half - sr_half)

        # Rebuild consumption for half-hour steps
        if hourly_consumption and len(hourly_consumption) == 24:
            consumption_per_step = []
            for i in range(half_steps):
                hidx = (sr_half + i) // 2 % 24
                frac = 0.5  # half-hour = half the hourly rate
                if i == 0:
                    frac *= (1.0 - (sunrise_hour * 2 - math.floor(sunrise_hour * 2)))
                elif i == half_steps - 1:
                    f = sunset_hour * 2 - math.floor(sunset_hour * 2)
                    frac *= f if f > 0 else 1.0
                consumption_per_step.append(hourly_consumption[hidx] * frac)
        else:
            flat_half = daytime_consumption_kwh / half_steps if half_steps > 0 else 0
            consumption_per_step = [flat_half] * half_steps

        solar_per_step = []
        for i in range(half_steps):
            sidx = (sr_half + i) % 48
            val = hourly_solar[sidx]
            if i == 0:
                elapsed_frac = sunrise_hour * 2 - math.floor(sunrise_hour * 2)
                val *= (1.0 - elapsed_frac) if elapsed_frac > 0 else 1.0
            elif i == half_steps - 1:
                f = sunset_hour * 2 - math.floor(sunset_hour * 2)
                if f > 0:
                    val *= f
            solar_per_step.append(val)

        steps = half_steps  # override step count for the simulation loop
    elif hourly_solar and len(hourly_solar) == 24:
        # Hourly Solcast forecast (fallback from detailedHourly)
        sr_int = int(math.floor(sunrise_hour))
        solar_per_step = []
        for i in range(steps):
            hour_idx = (sr_int + i) % 24
            val = hourly_solar[hour_idx]
            if i == 0:
                elapsed_frac = sunrise_hour - math.floor(sunrise_hour)
                val *= (1.0 - elapsed_frac) if elapsed_frac > 0 else 1.0
            elif i == steps - 1:
                f = sunset_hour - math.floor(sunset_hour)
                if f > 0:
                    val *= f
            solar_per_step.append(val)
    else:
        # Fallback: sine curve distribution
        solar_factors = [math.sin((i + 0.5) / steps * math.pi) for i in range(steps)]
        factor_sum = sum(solar_factors)
        if factor_sum > 0:
            solar_per_step = [(f / factor_sum) * solar_total_kwh for f in solar_factors]
        else:
            solar_per_step = [solar_total_kwh / steps] * steps

    battery = start_kwh
    surplus = 0.0
    morning_low = start_kwh
    net_positive_seen = False

    eff = inverter_efficiency if inverter_efficiency > 0 else 1.0

    for i in range(steps):
        consumption_dc = consumption_per_step[i] / eff
        battery += solar_per_step[i] - consumption_dc
        if battery > battery_cap:
            surplus += battery - battery_cap
            battery = battery_cap
        if battery < battery_floor:
            battery = battery_floor

        if not net_positive_seen:
            if solar_per_step[i] >= consumption_dc:
                net_positive_seen = True
            else:
                morning_low = min(morning_low, battery)

    return DaytimeResult(
        sunset_kwh=battery,
        surplus_kwh=surplus,
        morning_low_kwh=morning_low,
        total_consumption_kwh=round(sum(consumption_per_step), 2),
    )


def calc_overnight_drain_hourly(
    sunset_kwh: float,
    backup_charged_kwh: float,
    backup_kw: float,
    backup_activation_hour: int,
    hourly_consumption: list[float],
    sunset_hour: float,
    sunrise_hour: float,
    main_floor_kwh: float,
    main_cap: float,
    backup_mode: str = "always",
    target_kwh: float = 0.0,
    inverter_efficiency: float = 1.0,
    backup_discharge_efficiency: float = 1.0,
) -> float:
    """Calculate overnight drain using hourly consumption rates.

    Steps hour-by-hour from sunset to sunrise, applying each hour's
    consumption rate and backup battery assist after activation hour.
    """
    battery = sunset_kwh
    backup_remaining = backup_charged_kwh
    ss_int = int(math.floor(sunset_hour))
    sr_int = int(math.ceil(sunrise_hour)) % 24

    # Guard: if overnight is effectively zero, return sunset value
    if ss_int == sr_int:
        return max(main_floor_kwh, min(main_cap, sunset_kwh))

    def _is_backup_active(hour: int) -> bool:
        """Check if backup should be active at this hour."""
        if backup_kw <= 0 or backup_remaining <= 0:
            return False
        h24 = hour % 24
        # Backup active from activation_hour to sunrise (wrapping through midnight)
        if backup_activation_hour <= sr_int:
            return backup_activation_hour <= h24 < sr_int
        else:
            return h24 >= backup_activation_hour or h24 < sr_int

    def _drain_hour(bat: float, br: float, h: int, frac: float, use_backup: bool) -> tuple[float, float]:
        """Drain one hour of consumption from battery."""
        eff = inverter_efficiency if inverter_efficiency > 0 else 1.0
        consumption_dc = hourly_consumption[h % 24] * frac / eff

        if use_backup and br > 0 and backup_kw > 0 and _is_backup_active(h):
            backup_energy = min(br, backup_kw * frac)
            consumption_dc -= backup_energy * backup_discharge_efficiency / eff
            br -= backup_energy

        bat -= consumption_dc
        if bat < main_floor_kwh:
            bat = main_floor_kwh
        if bat > main_cap:
            bat = main_cap
        return bat, br

    def _run_overnight(use_backup: bool) -> float:
        """Simulate overnight drain, optionally with backup assist."""
        nonlocal backup_remaining
        bat = sunset_kwh
        br = backup_charged_kwh if use_backup else 0.0
        h = ss_int

        # First hour: fractional entry
        entry_frac = 1.0 - (sunset_hour - math.floor(sunset_hour))
        if h == sr_int:
            # Single hour overnight — just drain the overlap
            frac = max(0, sunrise_hour - sunset_hour)
            bat, br = _drain_hour(bat, br, h, frac, use_backup)
        else:
            bat, br = _drain_hour(bat, br, h, entry_frac, use_backup)
            h = (h + 1) % 24

            # Middle hours: full consumption
            for _ in range(24):
                if h == sr_int:
                    break
                bat, br = _drain_hour(bat, br, h, 1.0, use_backup)
                h = (h + 1) % 24

            # Sunrise hour: fractional exit
            if h == sr_int:
                exit_frac = sunrise_hour - math.floor(sunrise_hour)
                if exit_frac > 0:
                    bat, br = _drain_hour(bat, br, h, exit_frac, use_backup)

        if use_backup:
            backup_remaining = br
        return bat

    # For target-based mode, first check without backup
    if backup_mode == "target_based" and target_kwh > 0:
        no_backup = _run_overnight(use_backup=False)
        if no_backup >= target_kwh:
            return max(main_floor_kwh, min(main_cap, no_backup))
        # Need backup — run with it
        with_backup = _run_overnight(use_backup=True)
        if with_backup > target_kwh:
            return target_kwh
        return max(main_floor_kwh, min(main_cap, with_backup))

    # Always mode
    result = _run_overnight(use_backup=True)
    return max(main_floor_kwh, min(main_cap, result))


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
    inverter_efficiency: float = 1.0,
) -> float:
    """Calculate sunrise kWh with NO backup assist."""
    eff = inverter_efficiency if inverter_efficiency > 0 else 1.0
    main_kwh = sunset_kwh - (params.avg_overnight_kw * params.overnight_hours / eff)
    return max(main_floor_kwh, min(main_cap, main_kwh))


def calc_overnight_drain_with_backup(
    sunset_kwh: float,
    backup_charged_kwh: float,
    backup_kw: float,
    params: OvernightParams,
    main_floor_kwh: float,
    main_cap: float,
    inverter_efficiency: float = 1.0,
    backup_discharge_efficiency: float = 1.0,
) -> float:
    """Calculate sunrise kWh with backup assist (always mode)."""
    avg_kw = params.avg_overnight_kw
    eff = inverter_efficiency if inverter_efficiency > 0 else 1.0

    # Phase 1: sunset → backup activation, main covers all load
    main_kwh = sunset_kwh - (avg_kw * params.hours_sunset_to_activation / eff)

    # Phase 2: activation → sunrise, backup assists
    h2 = params.hours_activation_to_sunrise
    if backup_charged_kwh > 0 and backup_kw > 0 and h2 > 0:
        backup_hours = backup_charged_kwh / backup_kw
        net_kw = backup_kw * backup_discharge_efficiency / eff - avg_kw / eff
        if backup_hours >= h2:
            main_kwh += net_kw * h2
        else:
            main_kwh += net_kw * backup_hours
            remaining = h2 - backup_hours
            main_kwh -= avg_kw / eff * remaining
    else:
        main_kwh -= avg_kw / eff * h2

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
    inverter_efficiency: float = 1.0,
    backup_discharge_efficiency: float = 1.0,
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
            inverter_efficiency=inverter_efficiency,
            backup_discharge_efficiency=backup_discharge_efficiency,
        )

    # Target-based mode: first calculate without backup
    sunrise_no_backup = calc_overnight_drain_no_backup(
        sunset_kwh, params, main_floor_kwh, main_cap,
        inverter_efficiency=inverter_efficiency,
    )

    if sunrise_no_backup >= target_kwh or target_kwh <= 0:
        # Main can handle it alone — backup stays idle
        return sunrise_no_backup

    # Main falls short — calculate with full backup
    sunrise_with_backup = calc_overnight_drain_with_backup(
        sunset_kwh, backup_charged_kwh, backup_kw,
        params, main_floor_kwh, main_cap,
        inverter_efficiency=inverter_efficiency,
        backup_discharge_efficiency=backup_discharge_efficiency,
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
    hourly_consumption: list[float] | None = None,
    current_hour: float = 12.0,
    sunset_hour: float = 18.0,
    hourly_solar: list[float] | None = None,
    inverter_efficiency: float = 1.0,
    backup_charge_efficiency: float = 1.0,
    backup_discharge_efficiency: float = 1.0,
) -> DayResult:
    """Day 1 daytime prediction: current SoC + remaining solar → sunset → overnight.

    Uses partial sine curve from current hour to sunset, distributing
    remaining_solar across the remaining daylight hours proportionally.
    """
    sunrise_hour = sunset_hour - total_daytime_hours
    if sunrise_hour < 0:
        sunrise_hour = 0.0

    # Number of remaining steps from now to sunset
    current_h = int(math.floor(current_hour))
    sunset_h = int(math.ceil(sunset_hour))
    remaining_steps = max(1, sunset_h - current_h)

    # Full day steps (for sine curve positioning)
    sr_int = int(math.floor(sunrise_hour))
    full_steps = max(1, sunset_h - sr_int)

    # Build per-step consumption for remaining hours
    if hourly_consumption and len(hourly_consumption) == 24:
        consumption_per_step = []
        for i in range(remaining_steps):
            h = current_h + i
            hour_idx = h % 24
            if remaining_steps == 1:
                # Both start and end in same hour bucket
                frac = sunset_hour - current_hour
                if frac <= 0:
                    frac = 0.01
            elif i == 0:
                frac = 1.0 - (current_hour - math.floor(current_hour))
            elif i == remaining_steps - 1:
                frac = sunset_hour - math.floor(sunset_hour)
                if frac <= 0:
                    frac = 1.0
            else:
                frac = 1.0
            consumption_per_step.append(hourly_consumption[hour_idx] * frac)
    else:
        daytime_kwh = max(0, consumption.avg_daily_kwh - consumption.avg_overnight_kwh)
        if total_daytime_hours > 0:
            flat_rate = daytime_kwh / full_steps
        else:
            flat_rate = 0.0
        consumption_per_step = []
        for i in range(remaining_steps):
            if remaining_steps == 1:
                frac = sunset_hour - current_hour
                if frac <= 0:
                    frac = 0.01
            elif i == 0:
                frac = 1.0 - (current_hour - math.floor(current_hour))
            elif i == remaining_steps - 1:
                frac = sunset_hour - math.floor(sunset_hour)
                if frac <= 0:
                    frac = 1.0
            else:
                frac = 1.0
            consumption_per_step.append(flat_rate * frac)

    # Solar per step: use Solcast data if available, else sine curve
    if hourly_solar and len(hourly_solar) == 48:
        # Half-hourly Solcast: rebuild with half-hour steps from current time
        cur_half = int(math.floor(current_hour * 2))
        ss_half = int(math.ceil(sunset_hour * 2))
        remaining_steps = max(1, ss_half - cur_half)

        # Rebuild consumption for half-hour steps
        if hourly_consumption and len(hourly_consumption) == 24:
            consumption_per_step = []
            for i in range(remaining_steps):
                hidx = (cur_half + i) // 2 % 24
                frac = 0.5
                if remaining_steps == 1:
                    frac = sunset_hour - current_hour
                    if frac <= 0:
                        frac = 0.01
                elif i == 0:
                    frac *= (1.0 - (current_hour * 2 - math.floor(current_hour * 2)))
                elif i == remaining_steps - 1:
                    f = sunset_hour * 2 - math.floor(sunset_hour * 2)
                    frac *= f if f > 0 else 1.0
                consumption_per_step.append(hourly_consumption[hidx] * frac)
        else:
            flat_half = (max(0, consumption.avg_daily_kwh - consumption.avg_overnight_kwh)) / remaining_steps if remaining_steps > 0 else 0
            consumption_per_step = [flat_half] * remaining_steps

        solar_per_step = []
        for i in range(remaining_steps):
            sidx = (cur_half + i) % 48
            val = hourly_solar[sidx]
            if remaining_steps == 1:
                # Single step: scale by actual time span
                frac = (sunset_hour - current_hour) / 0.5
                val *= max(0, min(1, frac))
            elif i == 0:
                # First step: scale by fraction of half-hour remaining
                elapsed_frac = current_hour * 2 - math.floor(current_hour * 2)
                val *= (1.0 - elapsed_frac)
            elif i == remaining_steps - 1:
                # Last step: scale by fraction of half-hour before sunset
                f = sunset_hour * 2 - math.floor(sunset_hour * 2)
                if f > 0:
                    val *= f
            solar_per_step.append(val)
    elif hourly_solar and len(hourly_solar) == 24:
        # Hourly Solcast forecast for remaining hours
        solar_per_step = []
        for i in range(remaining_steps):
            hour_idx = (current_h + i) % 24
            val = hourly_solar[hour_idx]
            if remaining_steps == 1:
                frac = sunset_hour - current_hour
                val *= max(0, min(1, frac))
            elif i == 0:
                elapsed_frac = current_hour - math.floor(current_hour)
                val *= (1.0 - elapsed_frac)
            elif i == remaining_steps - 1:
                f = sunset_hour - math.floor(sunset_hour)
                if f > 0:
                    val *= f
            solar_per_step.append(val)
    else:
        # Fallback: distribute remaining_solar using tail of sine curve
        solar_factors = []
        for i in range(remaining_steps):
            step_in_day = (current_h + i) - sr_int
            t = (step_in_day + 0.5) / full_steps
            solar_factors.append(max(0, math.sin(t * math.pi)))

        factor_sum = sum(solar_factors)
        if factor_sum > 0:
            solar_per_step = [(f / factor_sum) * remaining_solar for f in solar_factors]
        else:
            solar_per_step = [remaining_solar / remaining_steps] * remaining_steps

    # Simulate hour by hour with clipping
    battery = current_kwh
    surplus = 0.0
    total_consumption = 0.0
    total_solar = 0.0
    eff = inverter_efficiency if inverter_efficiency > 0 else 1.0

    for i in range(remaining_steps):
        total_consumption += consumption_per_step[i]
        total_solar += solar_per_step[i]
        battery += solar_per_step[i] - consumption_per_step[i] / eff
        if battery > main.capacity_kwh:
            surplus += battery - main.capacity_kwh
            battery = main.capacity_kwh
        if battery < main.floor_kwh:
            battery = main.floor_kwh

    sunset_kwh = battery

    # Backup charge from solar surplus + current backup charge
    backup_current = 0.0
    if backup.enabled:
        backup_current = max(0, (backup_soc_pct - backup.floor_percent) / 100 * backup.capacity_kwh)

    backup_charged = min(backup.usable_kwh, backup_current + surplus * backup_charge_efficiency) if backup.enabled else 0.0

    # Overnight drain
    if hourly_consumption and len(hourly_consumption) == 24:
        sunrise_kwh = calc_overnight_drain_hourly(
            sunset_kwh=sunset_kwh,
            backup_charged_kwh=backup_charged,
            backup_kw=backup.fixed_discharge_kw if backup.enabled else 0,
            backup_activation_hour=backup.activation_hour if backup.enabled else 2,
            hourly_consumption=hourly_consumption,
            sunset_hour=sunset_hour,
            sunrise_hour=sunrise_hour,
            main_floor_kwh=main.floor_kwh,
            main_cap=main.capacity_kwh,
            backup_mode=backup.mode if backup.enabled else "always",
            target_kwh=target_kwh,
            inverter_efficiency=inverter_efficiency,
            backup_discharge_efficiency=backup_discharge_efficiency,
        )
    else:
        sunrise_kwh = calc_overnight_drain(
            sunset_kwh=sunset_kwh,
            backup_charged_kwh=backup_charged,
            backup_kw=backup.fixed_discharge_kw if backup.enabled else 0,
            params=overnight_params,
            main_floor_kwh=main.floor_kwh,
            main_cap=main.capacity_kwh,
            backup_mode=backup.mode if backup.enabled else "always",
            target_kwh=target_kwh,
            inverter_efficiency=inverter_efficiency,
            backup_discharge_efficiency=backup_discharge_efficiency,
        )

    return DayResult(
        soc_percent=round(sunrise_kwh / main.capacity_kwh * 100, 1),
        predicted_kwh=round(sunrise_kwh, 2),
        solcast_kwh=round(total_solar, 2),
        daytime_consumption_kwh=round(total_consumption, 2),
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
    inverter_efficiency: float = 1.0,
    backup_discharge_efficiency: float = 1.0,
    hourly_consumption: list[float] | None = None,
    sunrise_hour: float = 6.0,
) -> DayResult:
    """Day 1 nighttime prediction: current SoC - hourly drain + live backup."""
    eff = inverter_efficiency if inverter_efficiency > 0 else 1.0
    current_hour = now_dt.hour + now_dt.minute / 60

    # Use hourly consumption if available, otherwise fall back to flat rate
    if hourly_consumption and len(hourly_consumption) == 24:
        now_int = int(math.floor(current_hour))
        sr_int = int(math.floor(sunrise_hour))

        def _is_backup_active(h: int) -> bool:
            h24 = h % 24
            if activation_hour <= sr_int:
                return activation_hour <= h24 < sr_int
            else:
                return h24 >= activation_hour or h24 < sr_int

        def _drain_hour_d1(bat: float, br: float, h: int, frac: float) -> tuple[float, float]:
            """Drain one hour of consumption from battery."""
            consumption_dc = hourly_consumption[h % 24] * frac / eff
            if br > 0 and backup_kw > 0 and _is_backup_active(h):
                backup_energy = min(br, backup_kw * frac)
                consumption_dc -= backup_energy * backup_discharge_efficiency / eff
                br -= backup_energy
            bat -= consumption_dc
            bat = max(main.floor_kwh, min(main.capacity_kwh, bat))
            return bat, br

        bat = current_kwh
        br = backup_available_kwh

        if now_int == sr_int:
            # Same hour as sunrise — drain only the remaining fraction
            frac = max(0, sunrise_hour - current_hour)
            if frac > 0:
                bat, br = _drain_hour_d1(bat, br, now_int, frac)
        else:
            # First hour: fractional entry
            entry_frac = 1.0 - (current_hour - math.floor(current_hour))
            bat, br = _drain_hour_d1(bat, br, now_int, entry_frac)
            h = (now_int + 1) % 24

            # Middle hours: full consumption
            for _ in range(24):
                if h == sr_int:
                    break
                bat, br = _drain_hour_d1(bat, br, h, 1.0)
                h = (h + 1) % 24

            # Sunrise hour: fractional exit
            if h == sr_int:
                exit_frac = sunrise_hour - math.floor(sunrise_hour)
                if exit_frac > 0:
                    bat, br = _drain_hour_d1(bat, br, h, exit_frac)

        main_kwh = bat
    else:
        # Flat rate fallback
        avg_kw = overnight_params.avg_overnight_kw

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

        if hours_phase1 > 0:
            main_kwh -= avg_kw / eff * hours_phase1

        if hours_phase2 > 0:
            if backup_available_kwh <= 0 or backup_kw <= 0:
                main_kwh -= avg_kw / eff * hours_phase2
            else:
                backup_hours = backup_available_kwh / backup_kw
                net_kw = backup_kw * backup_discharge_efficiency / eff - avg_kw / eff
                if backup_hours >= hours_phase2:
                    main_kwh += net_kw * hours_phase2
                else:
                    main_kwh += net_kw * backup_hours
                    remaining = hours_phase2 - backup_hours
                    main_kwh -= avg_kw / eff * remaining

    main_kwh = max(main.floor_kwh, min(main.capacity_kwh, main_kwh))

    return DayResult(
        soc_percent=round(main_kwh / main.capacity_kwh * 100, 1),
        predicted_kwh=round(main_kwh, 2),
        backup_charged_kwh=round(backup_available_kwh, 2),
    )


def predict_future_day(
    prev_kwh: float,
    solar_kwh: float,
    consumption: ConsumptionData,
    main: BatteryConfig,
    backup: BackupConfig,
    overnight_params: OvernightParams,
    target_kwh: float = 0.0,
    hourly_consumption: list[float] | None = None,
    sunrise_hour: float = 6.0,
    sunset_hour: float = 18.0,
    hourly_solar: list[float] | None = None,
    inverter_efficiency: float = 1.0,
    backup_charge_efficiency: float = 1.0,
    backup_discharge_efficiency: float = 1.0,
) -> DayResult:
    """Predict SoC for Days 2-7 using hourly solar + consumption data."""
    daytime_kwh = max(0, consumption.avg_daily_kwh - consumption.avg_overnight_kwh)
    daytime_hours = 24.0 - overnight_params.overnight_hours
    if daytime_hours < 1:
        daytime_hours = 12.0

    # Simulate daytime with hourly consumption and solar if available
    sim = simulate_daytime(
        start_kwh=prev_kwh,
        solar_total_kwh=solar_kwh,
        daytime_consumption_kwh=daytime_kwh,
        daytime_hours=daytime_hours,
        battery_cap=main.capacity_kwh,
        battery_floor=main.floor_kwh,
        hourly_consumption=hourly_consumption,
        sunrise_hour=sunrise_hour,
        hourly_solar=hourly_solar,
        sunset_hour=sunset_hour,
        inverter_efficiency=inverter_efficiency,
    )
    sunset_kwh = sim.sunset_kwh

    # Backup charges from solar surplus (energy clipped when battery hit cap)
    backup_charged = min(backup.usable_kwh, sim.surplus_kwh * backup_charge_efficiency) if backup.enabled else 0.0

    # Use hourly overnight drain if hourly data available
    if hourly_consumption and len(hourly_consumption) == 24:
        sunrise_kwh = calc_overnight_drain_hourly(
            sunset_kwh=sunset_kwh,
            backup_charged_kwh=backup_charged,
            backup_kw=backup.fixed_discharge_kw if backup.enabled else 0,
            backup_activation_hour=backup.activation_hour if backup.enabled else 2,
            hourly_consumption=hourly_consumption,
            sunset_hour=sunset_hour,
            sunrise_hour=sunrise_hour,
            main_floor_kwh=main.floor_kwh,
            main_cap=main.capacity_kwh,
            backup_mode=backup.mode if backup.enabled else "always",
            target_kwh=target_kwh,
            inverter_efficiency=inverter_efficiency,
            backup_discharge_efficiency=backup_discharge_efficiency,
        )
    else:
        sunrise_kwh = calc_overnight_drain(
            sunset_kwh=sunset_kwh,
            backup_charged_kwh=backup_charged,
            backup_kw=backup.fixed_discharge_kw if backup.enabled else 0,
            params=overnight_params,
            main_floor_kwh=main.floor_kwh,
            main_cap=main.capacity_kwh,
            backup_mode=backup.mode if backup.enabled else "always",
            target_kwh=target_kwh,
            inverter_efficiency=inverter_efficiency,
            backup_discharge_efficiency=backup_discharge_efficiency,
        )

    return DayResult(
        soc_percent=round(sunrise_kwh / main.capacity_kwh * 100, 1),
        predicted_kwh=round(sunrise_kwh, 2),
        solcast_kwh=round(solar_kwh, 2),
        daytime_consumption_kwh=round(sim.total_consumption_kwh, 2),
        backup_charged_kwh=round(backup_charged, 2),
        morning_low_kwh=round(sim.morning_low_kwh, 2),
        morning_low_pct=round(sim.morning_low_kwh / main.capacity_kwh * 100, 1),
    )

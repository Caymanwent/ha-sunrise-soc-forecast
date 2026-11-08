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
class SocDumpLoad:
    """A dump load driven by battery SoC thresholds.

    Models an automation that engages a load when the battery is nearly full
    (e.g. >= 99%) and reverts when it drops below a lower threshold (e.g. < 96%).
    The simulation runs the same hysteresis state machine on the simulated
    battery, so the load's energy enters the forecast continuously — engaging
    a bit earlier when the forecast is sunnier — instead of flipping whole
    hours on a solar-vs-base threshold.
    """

    profile: list[float]  # 24-element kWh-per-hour drawn while the load runs
    engage_soc: float  # percent — simulated load turns on at/above this SoC
    revert_soc: float  # percent — simulated load turns off below this SoC
    initially_active: bool = False  # Day-1 seed from the live state entity


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
    floor_hour: str | None = None
    night_floor_hour: str | None = None
    # Full-battery prediction (3.4.0). The *_time floats are hours since the midnight
    # that the day's simulation counts from (monotonic through the midnight wrap, so a
    # value past 24 is the next calendar day); the *_hour strings are HH:MM (mod 24).
    # both_* is the later of main and backup, or just main when no backup is configured.
    main_full: bool = False
    backup_full: bool = False
    main_full_hour: str | None = None
    backup_full_hour: str | None = None
    both_full_hour: str | None = None
    main_full_time: float | None = None
    backup_full_time: float | None = None
    both_full_time: float | None = None


# Tolerance (kWh) for "battery is at capacity" comparisons on simulated floats
_FULL_EPS = 1e-6


def format_hhmm(hours: float | None) -> str | None:
    """HH:MM for an hours-since-midnight float, wrapped mod 24, to the nearest minute."""
    if hours is None:
        return None
    total_minutes = int(round(hours * 60)) % (24 * 60)
    h, m = divmod(total_minutes, 60)
    return f"{h:02d}:{m:02d}"


def _main_full_in_step(
    step_start: float,
    frac: float,
    before: float,
    net: float,
    cap: float,
    engaging: list[tuple[float, float]],
) -> float | None:
    """Hours float at which the main reaches ``cap`` inside a step, or None.

    The battery rises linearly from ``before`` by ``net`` over the step (``frac``
    hours). ``engaging`` lists SoC-gated dump loads that were OFF at the step start
    but whose engage level lies inside the step, as (engage_kwh, draw_kwh_per_hour):
    the simulation only switches those loads at step boundaries, so its energy
    accounting lets the battery run past them to the cap one step early. For the
    reported time the climb is re-traced piecewise, taking each load's draw off the
    rate from its engage level on. Returns None when a load would hold the battery
    below the cap — then the step's clip is a step-size artifact, not a full
    battery (a load that physically holds the main at 96-99 % all afternoon).
    Clamped to the step.
    """
    if before >= cap:
        return step_start
    if frac <= 0 or net <= 0:
        return None
    rate = net / frac
    level = before
    t = 0.0
    for engage_kwh, draw in sorted(engaging):
        if rate <= 0:
            return None
        t += (engage_kwh - level) / rate
        level = engage_kwh
        rate -= draw
    if rate <= 0:
        return None
    t += (cap - level) / rate
    return step_start + min(frac, max(0.0, t))


def _both_full_time(
    main_time: float | None, backup_time: float | None, backup_enabled: bool
) -> float | None:
    """The later of the two full times; the main's alone when no backup is configured."""
    if not backup_enabled:
        return main_time
    if main_time is not None and backup_time is not None:
        return max(main_time, backup_time)
    return None


def latch_full_times(
    result: DayResult,
    current_hour: float,
    main_since: float | None,
    backup_since: float | None,
    backup_enabled: bool,
) -> tuple[float | None, float | None]:
    """Pin a Day-1 result's full times to when each battery was FIRST seen full.

    predict_day1_daytime seeds a battery that is already full at the start with
    ``*_full_time == current_hour`` ("full as of now"), so once the batteries really
    are full every recalculation would move the reported time forward minute by
    minute. The coordinator keeps a latch per battery and passes it in here as
    hours on the same midnight as ``result`` (None = not latched): a latch is kept
    while the result still says "full now", set to ``current_hour`` the first time
    it does, and cleared when it does not (the live battery is below full — the
    time is then a prediction again). The result's main/backup/both fields are
    rewritten to the latched moments; the new latches are returned.
    """

    def _latch(full_time: float | None, since: float | None) -> float | None:
        if full_time is None or full_time > current_hour + _FULL_EPS:
            return None
        return since if since is not None else full_time

    main_since = _latch(result.main_full_time, main_since)
    backup_since = _latch(result.backup_full_time, backup_since) if backup_enabled else None

    if main_since is not None:
        result.main_full_time = main_since
        result.main_full_hour = format_hhmm(main_since)
    if backup_since is not None:
        result.backup_full_time = backup_since
        result.backup_full_hour = format_hhmm(backup_since)
    if main_since is not None or backup_since is not None:
        result.both_full_time = _both_full_time(
            result.main_full_time, result.backup_full_time, backup_enabled
        )
        result.both_full_hour = format_hhmm(result.both_full_time)
    return main_since, backup_since


@dataclass
class DaytimeResult:
    """Result of daytime simulation."""

    sunset_kwh: float
    surplus_kwh: float
    morning_low_kwh: float
    day_low_hour: str | None = None
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

    # Solar production per step: use forecast data if available, else sine curve
    if hourly_solar and len(hourly_solar) == 96:
        # Quarter-hourly forecast (Open-Meteo): 96 entries, run 4 steps per hour
        sr_qtr = int(math.floor(sunrise_hour * 4))
        ss_qtr = int(math.ceil(sunset_hour * 4))
        qtr_steps = max(1, ss_qtr - sr_qtr)

        # Rebuild consumption for quarter-hour steps
        if hourly_consumption and len(hourly_consumption) == 24:
            consumption_per_step = []
            for i in range(qtr_steps):
                hidx = (sr_qtr + i) // 4 % 24
                frac = 0.25  # quarter-hour = quarter the hourly rate
                if i == 0:
                    frac *= (1.0 - (sunrise_hour * 4 - math.floor(sunrise_hour * 4)))
                elif i == qtr_steps - 1:
                    f = sunset_hour * 4 - math.floor(sunset_hour * 4)
                    frac *= f if f > 0 else 1.0
                consumption_per_step.append(hourly_consumption[hidx] * frac)
        else:
            flat_qtr = daytime_consumption_kwh / qtr_steps if qtr_steps > 0 else 0
            consumption_per_step = [flat_qtr] * qtr_steps

        solar_per_step = []
        for i in range(qtr_steps):
            sidx = (sr_qtr + i) % 96
            val = hourly_solar[sidx]
            if i == 0:
                elapsed_frac = sunrise_hour * 4 - math.floor(sunrise_hour * 4)
                val *= (1.0 - elapsed_frac) if elapsed_frac > 0 else 1.0
            elif i == qtr_steps - 1:
                f = sunset_hour * 4 - math.floor(sunset_hour * 4)
                if f > 0:
                    val *= f
            solar_per_step.append(val)

        steps = qtr_steps
    elif hourly_solar and len(hourly_solar) == 48:
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
    day_low = start_kwh
    day_low_hour = None
    floor_hit = False

    # Determine step size in hours for time tracking
    if len(solar_per_step) == steps:
        step_hours = (sunset_hour - sunrise_hour) / steps if steps > 0 else 1.0
    else:
        step_hours = 1.0

    eff = inverter_efficiency if inverter_efficiency > 0 else 1.0

    for i in range(steps):
        consumption_dc = consumption_per_step[i] / eff
        battery += solar_per_step[i] - consumption_dc
        if battery > battery_cap:
            surplus += battery - battery_cap
            battery = battery_cap
        if battery < battery_floor:
            battery = battery_floor

        if battery < day_low:
            day_low = battery
            day_low_hour = sunrise_hour + (i + 1) * step_hours

        if battery <= battery_floor and not floor_hit:
            floor_hit = True
            day_low_hour = sunrise_hour + (i + 1) * step_hours

    # Format floor_hour as HH:MM
    floor_hour_str = None
    if floor_hit and day_low_hour is not None:
        h = int(day_low_hour)
        m = int((day_low_hour - h) * 60)
        floor_hour_str = f"{h:02d}:{m:02d}"

    return DaytimeResult(
        sunset_kwh=battery,
        surplus_kwh=surplus,
        morning_low_kwh=day_low,
        day_low_hour=floor_hour_str,
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
    sr_int = int(math.floor(sunrise_hour)) % 24

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

    def _run_overnight(use_backup: bool) -> tuple[float, str | None]:
        """Simulate overnight drain. Returns (sunrise_kwh, floor_hour or None)."""
        nonlocal backup_remaining
        bat = sunset_kwh
        br = backup_charged_kwh if use_backup else 0.0
        h = ss_int
        night_floor_hour = None

        def _step(bat_v, br_v, hour, frac):
            nonlocal night_floor_hour
            bat_v, br_v = _drain_hour(bat_v, br_v, hour, frac, use_backup)
            if bat_v <= main_floor_kwh and night_floor_hour is None:
                nh = (hour + 1) % 24
                night_floor_hour = f"{nh:02d}:00"
            return bat_v, br_v

        # First hour: fractional entry
        entry_frac = 1.0 - (sunset_hour - math.floor(sunset_hour))
        if h == sr_int:
            frac = max(0, sunrise_hour - sunset_hour)
            bat, br = _step(bat, br, h, frac)
        else:
            bat, br = _step(bat, br, h, entry_frac)
            h = (h + 1) % 24

            # Middle hours: full consumption
            for _ in range(24):
                if h == sr_int:
                    break
                bat, br = _step(bat, br, h, 1.0)
                h = (h + 1) % 24

            # Sunrise hour: fractional exit
            if h == sr_int:
                exit_frac = sunrise_hour - math.floor(sunrise_hour)
                if exit_frac > 0:
                    bat, br = _step(bat, br, h, exit_frac)

        if use_backup:
            backup_remaining = br
        return bat, night_floor_hour

    # For target-based mode, first check without backup
    if backup_mode == "target_based" and target_kwh > 0:
        no_backup, nfh = _run_overnight(use_backup=False)
        if no_backup >= target_kwh:
            return max(main_floor_kwh, min(main_cap, no_backup)), nfh
        with_backup, nfh = _run_overnight(use_backup=True)
        if with_backup > target_kwh:
            return target_kwh, nfh
        return max(main_floor_kwh, min(main_cap, with_backup)), nfh

    # Always mode
    result, nfh = _run_overnight(use_backup=True)
    return max(main_floor_kwh, min(main_cap, result)), nfh


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
    # Daily must be >= overnight — if not, data is corrupt
    sane = both_valid and daily_avg >= overnight_avg

    if sane:
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
    sunrise_hour: float,
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
    dump_load_profile: list[float] | None = None,
    soc_dump_loads: list[SocDumpLoad] | None = None,
) -> DayResult:
    """Day 1 prediction: continuous simulation from current time to next sunrise.

    One loop from now to sunrise — solar curve naturally tapers to zero after
    sunset, consumption and backup assist continue through the night. No separate
    daytime/nighttime phases or handoff.

    dump_load_profile carries loads gated per-hour on solar covering the base
    consumption. soc_dump_loads carries loads gated by the simulated battery
    SoC crossing their engage/revert thresholds (daylight only).

    Also records when the main first reaches capacity and when the backup
    (charged only from the main's surplus) first fills — see DayResult.
    """

    eff = inverter_efficiency if inverter_efficiency > 0 else 1.0

    # Determine step resolution based on solar data
    if hourly_solar and len(hourly_solar) == 96:
        steps_per_hour = 4
    elif hourly_solar and len(hourly_solar) == 48:
        steps_per_hour = 2
    else:
        steps_per_hour = 1

    step_hours = 1.0 / steps_per_hour
    array_len = len(hourly_solar) if hourly_solar else 24

    # Calculate start/end indices and total steps (wrapping through midnight)
    start_idx = int(math.floor(current_hour * steps_per_hour))
    end_idx = int(math.ceil(sunrise_hour * steps_per_hour))

    if hours_to_sunset > 0:
        # Daytime: ~24 hour simulation, always wraps through midnight
        total_steps = (array_len - start_idx) + end_idx
    elif end_idx < start_idx:
        # Nighttime past midnight: wraps (e.g., 23:00 → 06:00)
        total_steps = (array_len - start_idx) + end_idx
    else:
        # Nighttime before sunrise (or right at it): no wrap.
        # When start_idx == end_idx (current_hour ≈ sunrise_hour), this is 0 —
        # avoids a spurious 24h overnight drain at the sunrise boundary.
        total_steps = end_idx - start_idx

    if total_steps <= 0:
        total_steps = 0

    # Build solar values for sine fallback (full 24h, 0 outside solar window).
    # remaining_solar is what is still to come from current_hour onwards, so it is
    # spread over the part of the day's sine curve the loop will actually consume:
    # hours before current_hour get nothing, and the current hour's weight is scaled
    # by the fraction of it that is left (the loop pro-rates that first step the
    # same way), so the consumed total equals remaining_solar. Before sunrise that
    # is the whole curve; for Days 2-7 (which start at sunrise) only the sunrise
    # hour is down-weighted by the fraction of it after sunrise, matching the
    # loop's pro-rated first step.
    if not hourly_solar:
        sr_int = int(math.floor(sunrise_hour))
        ss_int = int(math.ceil(sunset_hour))
        full_day_steps = max(1, ss_int - sr_int)

        start_h = sr_int
        first_weight = 1.0
        if current_hour > sr_int:
            start_h = int(math.floor(current_hour))
            first_weight = 1.0 - (current_hour - start_h)

        sine_solar = [0.0] * 24
        solar_factors = []
        for h in range(sr_int, ss_int):
            if h < start_h:
                continue
            t = (h - sr_int + 0.5) / full_day_steps
            solar_factors.append((h, max(0, math.sin(t * math.pi))))
        factor_sum = sum(
            f * (first_weight if h == start_h else 1.0) for h, f in solar_factors
        )
        if factor_sum > 0 and remaining_solar > 0:
            for h, f in solar_factors:
                sine_solar[h] = (f / factor_sum) * remaining_solar
        solar_array = sine_solar
    else:
        solar_array = hourly_solar

    # Pre-compute total solar per hour for dump-load excess-solar gating.
    # Dump loads run only if solar covers the base (non-dump) consumption that hour.
    have_dump = bool(dump_load_profile) and len(dump_load_profile or []) == 24
    solar_per_hour: list[float] = [0.0] * 24
    if have_dump:
        for j, v in enumerate(solar_array):
            h = (j // steps_per_hour) % 24
            solar_per_hour[h] += v

    # SoC-gated dump loads: hysteresis state per load, seeded from the live
    # entity for Day 1 (initially_active). Only loads with a valid 24h profile.
    soc_loads = [
        ld for ld in (soc_dump_loads or [])
        if ld.profile and len(ld.profile) == 24
    ]
    soc_load_active = [ld.initially_active for ld in soc_loads]

    # Backup setup
    backup_current = 0.0
    if backup.enabled:
        backup_current = max(0, (backup_soc_pct - backup.floor_percent) / 100 * backup.capacity_kwh)
    br = backup_current
    backup_kw = backup.fixed_discharge_kw if backup.enabled else 0.0
    activation_hour = backup.activation_hour if backup.enabled else 2

    def _is_backup_active(h: int) -> bool:
        if not backup.enabled or backup_kw <= 0:
            return False
        h24 = h % 24
        sr = int(math.floor(sunrise_hour))
        if activation_hour <= sr:
            return activation_hour <= h24 < sr
        else:
            return h24 >= activation_hour or h24 < sr

    # Continuous simulation: current_hour → sunrise_hour
    battery = current_kwh
    surplus = 0.0
    day_low = current_kwh
    day_low_time = None
    floor_hit = False
    total_consumption = 0.0
    total_solar = 0.0

    # Full-battery tracking: the first time the main reaches capacity and the
    # backup reaches its usable capacity (charged from the main's surplus), as
    # hours since this simulation day's midnight. Recorded only on solar steps;
    # a battery that is already full at the start is "full at the start time".
    main_full_time: float | None = None
    backup_full_time: float | None = None
    backup_tracked = backup.enabled and backup.usable_kwh > 0
    if current_kwh >= main.capacity_kwh - _FULL_EPS:
        main_full_time = current_hour
    if backup_tracked and br >= backup.usable_kwh - _FULL_EPS:
        backup_full_time = current_hour

    for i in range(total_steps):
        idx = (start_idx + i) % array_len
        hour = (idx // steps_per_hour) % 24

        # Fractional first/last steps
        if total_steps == 1:
            frac = max(0.001, sunrise_hour - current_hour)  # actual remaining time
        elif i == 0:
            elapsed = current_hour * steps_per_hour - math.floor(current_hour * steps_per_hour)
            frac = step_hours * (1.0 - elapsed)
        elif i == total_steps - 1:
            end_frac = sunrise_hour * steps_per_hour - math.floor(sunrise_hour * steps_per_hour)
            frac = step_hours * end_frac if end_frac > 0 else step_hours
        else:
            frac = step_hours

        if frac <= 0:
            continue

        # Wall-clock span of this step (hours since the start day's midnight, not
        # wrapped, so it keeps rising through midnight): the first step begins
        # at the current time, later ones on their array boundary.
        step_start = current_hour if i == 0 else (start_idx + i) * step_hours

        # Solar (already 0 for nighttime indices). The final step ends at the next
        # sunrise, i.e. it lies before sunrise, so it carries no solar even though
        # its array slot is this morning's sunrise slot (the loop wraps a full day).
        solar_val = solar_array[idx] if idx < len(solar_array) else 0.0
        if i == total_steps - 1:
            solar_val = 0.0
        solar_scaled = solar_val * (frac / step_hours) if step_hours > 0 else 0.0

        # Update SoC-gated dump-load state machines — hysteresis on the
        # simulated battery, daylight only (these loads soak solar surplus
        # and their automations revert at sunset).
        if soc_loads:
            soc_pct_now = (
                battery / main.capacity_kwh * 100 if main.capacity_kwh > 0 else 0.0
            )
            for k, ld in enumerate(soc_loads):
                if solar_val <= 0:
                    soc_load_active[k] = False
                elif soc_load_active[k]:
                    if soc_pct_now < ld.revert_soc:
                        soc_load_active[k] = False
                elif soc_pct_now >= ld.engage_soc:
                    soc_load_active[k] = True

        # Consumption — remove each dump load's energy from the recorded
        # bucket (capped at what the bucket actually holds), then add it
        # back per its gate. Solar-gated loads run only if solar covers the
        # base that hour; SoC-gated loads run only while their simulated
        # state machine is on. Removal caps keep the simulated hour from
        # ever exceeding the recorded bucket.
        soc_load_draw = [0.0] * len(soc_loads)  # kWh/h each SoC load adds this hour when on
        if hourly_consumption and len(hourly_consumption) == 24:
            remainder = hourly_consumption[hour]
            removed_solar_gated = 0.0
            if have_dump and dump_load_profile[hour] > 0:
                removed_solar_gated = min(dump_load_profile[hour], remainder)
                remainder -= removed_solar_gated
            soc_dump_hour = 0.0
            for k, ld in enumerate(soc_loads):
                if ld.profile[hour] > 0:
                    removed = min(ld.profile[hour], remainder)
                    remainder -= removed
                    soc_load_draw[k] = removed
                    if soc_load_active[k]:
                        soc_dump_hour += removed
            base = remainder
            cons_kwh = base + soc_dump_hour
            if removed_solar_gated > 0 and solar_per_hour[hour] >= base:
                cons_kwh += removed_solar_gated
            cons_dc = cons_kwh * frac / eff
        else:
            cons_dc = 0.0

        # Backup assist
        if br > 0 and backup_kw > 0 and _is_backup_active(hour):
            backup_energy = min(br, backup_kw * frac)
            cons_dc -= backup_energy * backup_discharge_efficiency / eff
            br -= backup_energy

        total_solar += solar_scaled
        # Only count consumption during solar hours for the daytime attribute
        if solar_val > 0 and hourly_consumption:
            total_consumption += hourly_consumption[hour] * frac

        battery_before = battery
        net = solar_scaled - cons_dc
        battery += net
        if (
            main_full_time is None
            and solar_val > 0
            and battery >= main.capacity_kwh - _FULL_EPS
        ):
            # Interpolate the crossing inside the step from the step's net gain.
            # SoC-gated loads that are off now but whose engage level lies inside
            # this step are taken into account for the reported time only (the
            # energy accounting above switches them at the next step boundary);
            # a load engaging at 100 % cannot keep the battery from getting there.
            engaging = [
                (ld.engage_soc / 100 * main.capacity_kwh, soc_load_draw[k] / eff)
                for k, ld in enumerate(soc_loads)
                if not soc_load_active[k]
                and battery_before < ld.engage_soc / 100 * main.capacity_kwh <= battery
                and ld.engage_soc / 100 * main.capacity_kwh < main.capacity_kwh - _FULL_EPS
            ]
            main_full_time = _main_full_in_step(
                step_start, frac, battery_before, net, main.capacity_kwh, engaging
            )
        if battery > main.capacity_kwh:
            clip = battery - main.capacity_kwh
            surplus += clip
            # Charge backup from surplus in real-time
            if backup.enabled and br < backup.usable_kwh:
                available = clip * backup_charge_efficiency
                needed = backup.usable_kwh - br
                charge = min(available, needed)
                br += charge
                if (
                    backup_full_time is None
                    and backup_tracked
                    and solar_val > 0
                    and main_full_time is not None
                    and available >= needed - _FULL_EPS
                ):
                    # Surplus only accrues once the main is full: the part of the
                    # step that went into the main first (none when it was full
                    # before this step), then the share of the step's surplus the
                    # backup still needed.
                    main_frac = min(1.0, max(0.0, (main_full_time - step_start) / frac))
                    charge_frac = min(1.0, needed / available) if available > 0 else 1.0
                    backup_full_time = step_start + frac * (
                        main_frac + (1.0 - main_frac) * charge_frac
                    )
            battery = main.capacity_kwh
        if battery < main.floor_kwh:
            battery = main.floor_kwh

        # Track day low and floor hour
        if battery < day_low:
            day_low = battery
            step_time = current_hour + (i + 1) * step_hours
            day_low_time = step_time

        if battery <= main.floor_kwh and not floor_hit:
            floor_hit = True
            step_time = current_hour + (i + 1) * step_hours
            day_low_time = step_time

    # Format floor_hour as HH:MM (handles wrap past 24)
    floor_hour_str = None
    if floor_hit and day_low_time is not None:
        t = day_low_time % 24
        h = int(t)
        m = int((t - h) * 60)
        floor_hour_str = f"{h:02d}:{m:02d}"

    sunrise_kwh = battery

    # Backup charge for Days 2-7 grid_needed reference
    backup_charged = min(backup.usable_kwh, backup_current + surplus * backup_charge_efficiency) if backup.enabled else 0.0

    # Both full = the later of the two; with no backup configured, the main alone
    if not backup.enabled:
        backup_full_time = None
    main_full = main_full_time is not None
    backup_full = backup_full_time is not None
    both_full_time = _both_full_time(main_full_time, backup_full_time, backup.enabled)

    return DayResult(
        soc_percent=round(sunrise_kwh / main.capacity_kwh * 100, 1),
        predicted_kwh=round(sunrise_kwh, 2),
        solcast_kwh=round(total_solar, 2),
        daytime_consumption_kwh=round(total_consumption, 2),
        backup_charged_kwh=round(backup_charged, 2),
        morning_low_kwh=round(day_low, 2),
        morning_low_pct=round(day_low / main.capacity_kwh * 100, 1),
        floor_hour=floor_hour_str,
        main_full=main_full,
        backup_full=backup_full,
        main_full_hour=format_hhmm(main_full_time),
        backup_full_hour=format_hhmm(backup_full_time),
        both_full_hour=format_hhmm(both_full_time),
        main_full_time=main_full_time,
        backup_full_time=backup_full_time,
        both_full_time=both_full_time,
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
    dump_load_profile: list[float] | None = None,
    soc_dump_loads: list[SocDumpLoad] | None = None,
) -> DayResult:
    """Predict SoC for Days 2-7 using the same continuous loop as Day 1."""
    return predict_day1_daytime(
        current_kwh=prev_kwh,
        remaining_solar=solar_kwh,
        hours_to_sunset=sunset_hour - sunrise_hour,
        sunrise_hour=sunrise_hour,
        consumption=consumption,
        main=main,
        backup=backup,
        backup_soc_pct=backup.floor_percent,
        overnight_params=overnight_params,
        target_kwh=target_kwh,
        hourly_consumption=hourly_consumption,
        current_hour=sunrise_hour,
        sunset_hour=sunset_hour,
        hourly_solar=hourly_solar,
        inverter_efficiency=inverter_efficiency,
        backup_charge_efficiency=backup_charge_efficiency,
        backup_discharge_efficiency=backup_discharge_efficiency,
        dump_load_profile=dump_load_profile,
        soc_dump_loads=soc_dump_loads,
    )

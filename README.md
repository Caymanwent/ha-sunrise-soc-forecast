# Sunrise SoC Forecast for Home Assistant

Predicts your battery's State of Charge (SoC) at sunrise for up to 7 days ahead. Helps you plan energy usage, schedule grid charging, and avoid running out of battery overnight.

## What It Does

Every day, this integration answers one question: **what will my battery be at when the sun comes up?**

It combines your current battery state, solar forecasts from Solcast, and your actual consumption patterns to predict the SoC at sunrise for tomorrow and the next 6 days.

### Example Dashboard

| Day | Status | SoC % | kWh | Solcast | Grid Need |
|-----|--------|-------|-----|---------|-----------|
| Tomorrow | 🟢 | 62% | 79.4 kWh | 180 kWh | 0 kWh |
| Day 2 | 🟢 | 62% | 79.4 kWh | 210 kWh | 0 kWh |
| Day 3 | 🟡 | 12% | 15.4 kWh | 50 kWh | 10.2 kWh |
| Day 4 | 🟢 | 58% | 74.2 kWh | 195 kWh | 0 kWh |

At a glance you can see that Day 3 has a weak solar forecast and the battery will be low — you might want to schedule grid charging the night before.

## How It Works

### During the Day (sunrise to sunset)

The integration takes your **current battery SoC**, adds the **remaining solar forecast** from Solcast, subtracts the **remaining daytime consumption** (proportional to hours until sunset), and projects what the battery will be at sunset. It then models the overnight drain to predict sunrise SoC.

As the day progresses, the prediction naturally converges toward the actual value — current SoC rises as solar charges the battery, remaining solar decreases, and remaining consumption shrinks.

### During the Night (sunset to sunrise)

The integration takes the **current battery SoC** and subtracts the expected overnight drain using your 7-day average overnight consumption rate. If you have a backup battery, it models the assist based on your chosen deployment mode (see Backup Battery section below).

The prediction updates every time your battery SoC sensor changes (typically every 30-60 seconds), so it self-corrects continuously.

### Days 2-7

Each day chains from the previous day's prediction. Day 2 starts with Day 1's predicted sunrise kWh, adds that day's Solcast forecast, subtracts daytime consumption, then models overnight drain. Day 3 chains from Day 2, and so on.

At sunset, Days 2-7 values are frozen to prevent jumps when Solcast entities shift at midnight. They refresh at the next sunrise with updated data.

## Features

- **7-day SoC forecast** with per-day Solcast solar forecasts
- **Daytime + nighttime modes** for Day 1 with live sensor data
- **Backup battery modeling** (optional) with two deployment modes
- **Self-averaging consumption** — tracks 7-day rolling averages internally, no external helpers needed
- **Grid import tracking** (optional) — shows how much grid power is needed to reach your target SoC for all 7 days
- **Target SoC planning** — set a desired sunrise SoC and see the grid deficit for each day
- **Overnight freeze** — Days 2-7 hold steady overnight, no midnight jumps
- **Persistent state** — consumption history, accumulators, and frozen data survive restarts
- **UI configuration** — 4-step setup wizard, no YAML editing required

## Prerequisites

- **Home Assistant** 2024.8 or newer
- **Solcast PV integration** — provides solar forecast entities ([Solcast Solar](https://github.com/BJReplay/ha-solcast-solar) or similar)
- **Battery sensors** — SoC (%) and house load power (W) sensors from your inverter integration

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three-dot menu → **Custom repositories**
3. Add the repository URL, category: **Integration**
4. Search for "Sunrise SoC Forecast" and download
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/sunrise_soc_forecast/` folder to your Home Assistant `custom_components/` directory
2. Restart Home Assistant

## Setup

After installation, go to **Settings → Devices & Services → Add Integration** and search for **Sunrise SoC Forecast**.

### Step 1: Main Battery

| Field | Description | Example |
|-------|-------------|---------|
| Battery SoC sensor | Your battery's state of charge (%) | `sensor.battery_soc` |
| House load power sensor | Total house consumption (W) | `sensor.house_load_power` |
| Battery capacity | Total capacity in kWh | `128` |
| Minimum discharge floor | Lowest SoC the battery will reach (%) | `5` |
| Grid power sensor (optional) | Grid import power (W) for tracking grid usage | `sensor.grid_import_power` |

The house load power sensor is used for two things: predicting overnight drain rates and internally tracking your daily and overnight energy consumption (no external helpers or utility meters needed).

The grid power sensor is optional. If provided, the integration tracks how much grid energy you've imported today and calculates how much more is needed to reach your target.

### Step 2: Backup Battery (Optional)

Enable this if you have a secondary battery that assists overnight (e.g., Powerwall, secondary inverter bank).

| Field | Description | Example |
|-------|-------------|---------|
| Backup SoC sensor | Backup battery state of charge (%) | `sensor.backup_battery_soc` |
| Backup discharge sensor | Backup discharge power (kW) | `sensor.backup_discharge_power` |
| Backup capacity | Total capacity in kWh | `16` |
| Minimum discharge floor | Lowest SoC (%) | `5` |
| Fixed discharge rate | Expected discharge rate for forecast (kW) | `6.0` |
| Activation hour | Hour the backup starts discharging (24h) | `2` (= 2am) |
| Deployment mode | How the backup is used overnight | See below |

#### Backup Deployment Modes

| Mode | Behavior |
|------|----------|
| **Always discharge nightly** | Backup activates at the set hour every night and drains to floor. The main battery benefits from reduced load during the backup's active period. Use this if your backup always cycles overnight. |
| **Only if needed to reach target SoC** | Backup stays idle if the main battery can reach the target SoC on its own. Only activates when the predicted sunrise SoC (without backup) would fall below the target. Discharges just enough to bring main up to target — preserving backup for emergencies or days that actually need it. |

### Step 3: Solcast Entities

Select your Solcast PV forecast sensors. You need one for each forecast period:

| Field | Typical Entity | Required |
|-------|---------------|----------|
| Remaining today | `sensor.solcast_pv_forecast_remaining_today` | Yes |
| Forecast today (hourly detail) | `sensor.solcast_pv_forecast_today` | Optional |
| Forecast tomorrow | `sensor.solcast_pv_forecast_tomorrow` | Yes |
| Forecast day 3 | `sensor.solcast_pv_forecast_day_3` | Yes |
| Forecast day 4 | `sensor.solcast_pv_forecast_day_4` | Yes |
| Forecast day 5 | `sensor.solcast_pv_forecast_day_5` | Yes |
| Forecast day 6 | `sensor.solcast_pv_forecast_day_6` | Yes |
| Forecast day 7 | `sensor.solcast_pv_forecast_day_7` | Yes |

**Forecast today (recommended)**: If your Solcast integration provides a "forecast today" entity with `detailedForecast` or `detailedHourly` attributes, adding it significantly improves accuracy. The integration uses the actual half-hourly solar production curve instead of a sine approximation, which gives much more accurate morning low (AM Low) predictions.

### Step 4: Options

| Field | Description | Default |
|-------|-------------|---------|
| Forecast days | Number of days to predict (2-7) | `7` |
| Target SoC | Desired minimum SoC at sunrise (%) — used for grid import and backup calculations | `20` |
| Fallback daily consumption | Used until real data accumulates (kWh) | `128` |
| Fallback overnight consumption | Used until real data accumulates (kWh) | `64` |
| Guard threshold | Minimum kWh for averages to be considered valid | `3` |

## Sensors Created

After setup, the integration creates these sensors:

| Sensor | Description |
|--------|-------------|
| `sensor.predicted_sunrise_soc_day_1` | Tomorrow's sunrise SoC prediction (%) |
| `sensor.predicted_sunrise_soc_day_2` through `day_7` | Days 2-7 predictions |
| `sensor.7_day_average_daily_consumption` | Rolling 7-day daily consumption average (kWh) |
| `sensor.7_day_average_overnight_consumption` | Rolling 7-day overnight consumption average (kWh) |

### Sensor Attributes (All Days)

| Attribute | Description |
|-----------|-------------|
| `predicted_kwh` | Predicted battery energy at sunrise (kWh) |
| `solcast_kwh` | Solcast forecast used for that day (kWh) |
| `daytime_consumption_kwh` | Daytime consumption estimate (kWh) |
| `backup_charged_kwh` | Backup battery charge available for that night (kWh) |
| `grid_needed_kwh` | Grid import needed to reach target SoC (kWh) |
| `grid_used_today_kwh` | Grid import accumulated today (kWh) — Day 1 only |
| `target_soc` | Configured target SoC (%) |
| `morning_low_kwh` | Predicted lowest battery level before solar ramps up (kWh) — Days 2-7 |
| `morning_low_pct` | Same as percentage — Days 2-7 |

### Consumption Average Attributes

| Attribute | Description |
|-----------|-------------|
| `history` | List of daily values (up to 7) |
| `days` | Number of days in the history |
| `hourly_averages` | 24-element list of per-hour consumption averages (kWh/h) |

### Day 1 Additional Attributes

| Attribute | Description |
|-----------|-------------|
| `mode` | Current mode: `daytime` or `nighttime` |
| `remaining_solar_kwh` | Solcast remaining today (daytime only) |
| `using_fallback` | Whether consumption fallbacks are active |

## Grid Import Planning

Each day's sensor shows `grid_needed_kwh` — how much grid import would be needed for the main battery to reach the target SoC at sunrise. This lets you plan ahead:

- **Days 2-7**: Shows the forecasted grid need based on predicted SoC. Use this to plan which nights to schedule grid charging.

```
grid_needed     = (target_soc% × capacity) - predicted_sunrise_kwh

```

`grid_needed_kwh` updates live. As grid import charges the battery, the SoC prediction improves and `grid_needed` drops toward zero.

## Backup Battery Behavior

### Always Mode

The overnight is split into two phases:

**Phase 1** (sunset → activation hour): The main battery covers all house load, draining at the average overnight rate.

**Phase 2** (activation hour → sunrise): The backup battery discharges to cover the house load. When the backup runs out, the main battery takes over.

```
Example: 80 kWh main, 10 kWh backup, activation at 2am

Phase 1: 4h to 2am @ 4 kW → 80 - 16 = 64 kWh
Phase 2: 4.4h, backup = 10 kWh @ 6 kW
  Backup runs 1.67h: 64 + (net × 1.67) = 67.3 kWh
  Backup empty, 2.73h: 67.3 - (4 × 2.73) = 56.4 kWh
Sunrise: 56.4 kWh = 44.1%
```

### Target-Based Mode

1. First calculates sunrise SoC **without** backup assist
2. If above target → backup stays idle, preserving charge
3. If below target → backup activates and discharges just enough to bring main up to target
4. If even full backup can't reach target → uses all available backup

This is useful for systems where you want to preserve the backup for emergencies or only use it when genuinely needed.

## Consumption Tracking

The integration tracks your energy consumption internally — no external helpers, utility meters, or Riemann sums needed.

### Hourly Consumption Model

Consumption is tracked in **24 hourly buckets** (one per hour of the day). Each bucket builds a 7-day rolling average for that specific hour. This captures real consumption patterns:

- **Evening hours** (6-10 PM): typically higher consumption (cooking, HVAC, entertainment)
- **Overnight hours** (1-5 AM): typically lower consumption (standby loads only)
- **Morning hours** (6-9 AM): moderate consumption spike

The hourly averages are used by the prediction model to simulate each hour of the day individually, rather than using flat daytime/overnight rates. This produces more accurate sunset SoC predictions and morning low estimates.

### How It Accumulates

- House load power sensor is sampled on every state change (~30-60 seconds)
- Energy is accumulated via trapezoidal integration into the current hour's bucket
- At each hour boundary, the completed hour's energy is recorded into its 7-day history
- Daily and overnight totals are also tracked for backward compatibility

### Fallback Behavior

Until real hourly data accumulates (first week), the integration uses flat rates:
- **Daytime hours**: flat rate derived from fallback daily consumption
- **Overnight hours**: flat rate derived from fallback overnight consumption

As each hour gets its first real data point, it switches from fallback to real. After 7 days, all 24 hours use real rolling averages.

All consumption data (including hourly histories) persists across HA restarts.

## Dashboard Card

A complete dashboard card is included in `dashboard.yaml`. To use it:

1. Open your HA dashboard → Edit → Add Card → Manual
2. Paste the contents of `dashboard.yaml`

The card includes:
- Dynamic row labels (Tomorrow / This Morning)
- Status indicators (🟢 above 15%, 🟡 5-15%, 🔴 below 5%)
- All 7 days with SoC, kWh, Solcast, Daytime consumption, Backup, and Grid needed
- Mode indicator (Daytime/Nighttime) with remaining solar and hours to sunset
- Grid tracking summary when there's a deficit
- Consumption source indicator (fallback vs 7-day averages)
- Automatically skips days with no data

## Automation Examples

### Grid Charge When Forecast is Low

```yaml
automation:
  - alias: "Grid charge when forecast is low"
    trigger:
      - platform: numeric_state
        entity_id: sensor.predicted_sunrise_soc_day_1
        below: 20
    condition:
      - condition: numeric_state
        entity_id: sensor.predicted_sunrise_soc_day_1
        above: 0
    action:
      - action: switch.turn_on
        target:
          entity_id: switch.grid_charger
```

### Plan Ahead: Grid Charge for Day 3

```yaml
automation:
  - alias: "Schedule grid charge for weak Day 3"
    trigger:
      - platform: numeric_state
        entity_id: sensor.predicted_sunrise_soc_day_3
        attribute: grid_needed_kwh
        above: 10
    action:
      - action: notify.mobile_app
        data:
          title: "Grid Charge Recommended"
          message: >
            Day 3 forecast needs {{ state_attr('sensor.predicted_sunrise_soc_day_3',
            'grid_needed_kwh') }} kWh from grid. Consider scheduling overnight charging.
```

### Low Battery Forecast Alert

```yaml
automation:
  - alias: "Low battery forecast alert"
    trigger:
      - platform: numeric_state
        entity_id:
          - sensor.predicted_sunrise_soc_day_1
          - sensor.predicted_sunrise_soc_day_2
          - sensor.predicted_sunrise_soc_day_3
          - sensor.predicted_sunrise_soc_day_4
          - sensor.predicted_sunrise_soc_day_5
          - sensor.predicted_sunrise_soc_day_6
          - sensor.predicted_sunrise_soc_day_7
        below: 10
    action:
      - action: notify.mobile_app
        data:
          title: "Low Battery Forecast"
          message: >
            {{ trigger.to_state.name }} predicted at
            {{ trigger.to_state.state }}% at sunrise.
            Grid needed: {{ state_attr(trigger.entity_id, 'grid_needed_kwh') }} kWh
```

## Upgrading

When updating to a new version, all consumption history and settings are preserved. No reconfiguration is required.

### Recommended after upgrading to v2.5+

For improved prediction accuracy, add the **Forecast Today** Solcast entity via the configure button (Settings → Devices & Services → Sunrise SoC Forecast → Configure). This enables half-hourly solar production curves from Solcast, replacing the sine approximation. The improvement is most noticeable in morning low (AM Low) predictions where the sine curve overestimates early morning solar by up to 7x.

If your Solcast integration doesn't provide a "forecast today" entity with detailed hourly data, the integration continues to work using the sine curve — no action needed.

## Troubleshooting

### All days show the same value
This is normal when using fallback consumption values (first 7 days). With high solar and the default 128/64 kWh consumption, the battery fills to capacity every day and drains the same amount overnight. Once real consumption data accumulates, predictions will differentiate based on varying Solcast forecasts.

### Consumption averages say "unknown"
The averages need at least one midnight (daily) and one sunrise (overnight) trigger to have data. Wait 24 hours after installation.

### Days 2-7 show 0% after restart
This happens if frozen data hasn't been captured yet. It resolves at the next sunset when values are frozen. During daytime, they recalculate live.

### Prediction seems too high/low
Check the `using_fallback` attribute on Day 1. If `true`, the integration is using your configured fallback values instead of real consumption data. Adjust the fallback values in the integration options to better match your system, or wait 7 days for real data to accumulate.

### Grid needed shows 0 for all days
Either your predicted SoC is above the target for all days (good!), or the target SoC is set to 0. Check the `target_soc` attribute on any day's sensor.

## License

MIT

# Sigen cloud API notes

What this server calls, and what the fields mean. Sigen doesn't document these endpoints: they're
the ones the mySigen app uses, so they can change without notice. Field meanings below were checked
by comparing the data with the mySigen app's charts.

All requests go to the regional base URL, for example `https://api-eu.sigencloud.com/`, with the
bearer token that the [`sigen`](https://github.com/fbradyirl/sigen) library gets at login.

## Endpoints

| Purpose | Method and path | Used by |
|---|---|---|
| Log in / refresh token | `POST auth/oauth/token` | `sigen` library |
| Station details | `GET device/owner/station/home` | `station_info` |
| Live power flow | `GET device/sigen/station/energyflow?id={stationId}` | `energy_flow` |
| Current mode | `GET device/energy-profile/mode/current/{stationId}` | `operating_mode` |
| All modes | `GET device/energy-profile/mode/all/{stationId}` | `operating_mode` |
| One day's history | `GET data-process/sigen/station/statistics/energy` | `energy_history`, `day_summary`, `date_range_summary` |
| Smart loads | `GET device/system/device/systemDevice/card` and `device/tp-device/smart-loads` | `smart_loads` |

### Day history parameters

| Parameter | Value |
|---|---|
| `stationId` | from station details |
| `dateFlag` | `1` (one day). `0` is rejected. |
| `startDate`, `endDate` | the same day, as `YYYYMMDD`. Dashes give a server error. |
| `fulfill` | `false` |

The response has the day's totals in kWh, plus `itemList`: 288 readings, one every 5 minutes, at
local station time (`dataTime` like `20261008 17:40`).

## History reading fields (`itemList`)

Powers are 5-minute averages in kW. Multiply by 5/60 to get kWh.

| Field | Meaning |
|---|---|
| `pvTotalPower` | Solar generation |
| `loadPower` | House consumption |
| `fromGridPower` | Grid import, **including** grid power charging the battery |
| `toGridPower` | **Solar** export only. Battery export is not included here. |
| `esChargeDischargePower` | Battery **total** power: + charging, − discharging. Includes battery→grid. |
| `esChargePower` | Battery charging power |
| `esDischargePower` | Battery → **house** only |
| `batSoc` | Battery state of charge, % |
| `powerGeneration`, `powerUse`, `powerFromGrid`, `powerToGrid`, `esCharging`, `esDischarging` | Running totals for the day so far, kWh |

Derived values used by the summaries:

- **Battery export** = `max(0, −esChargeDischargePower − esDischargePower)`
- **Grid → battery** = `max(0, min(max(0, esChargeDischargePower), fromGridPower − loadPower))`
- **Grid → house** = `fromGridPower − grid → battery`

Watch out for the daily totals. Sigen's `powerFromGrid` and `powerToGrid` leave out grid charging
and battery export. For example, a day with 31 kWh of real grid import showed `powerFromGrid` as
16 kWh. Add up the 5-minute readings instead.

## Live flow fields (`energy_flow`)

| Field | Meaning |
|---|---|
| `pvPower`, `loadPower` | Solar and house, kW |
| `batteryPower` | + charging, − discharging, kW |
| `batterySoc` | % |
| `buySellPower` | **Negative = importing** from the grid, positive = exporting |
| `pvDayNrg` | Solar energy today, kWh |

## Tariff prices (Octopus)

When `TARIFF_SOURCE=octopus`, prices come from Octopus's public API. No login is needed.

- Unit rates: `GET https://api.octopus.energy/v1/products/{product}/electricity-tariffs/E-1R-{product}-{region}/standard-unit-rates/`
- Standing charge: `…/standing-charges/` on the import tariff
- Region from postcode: `GET https://api.octopus.energy/v1/industry/grid-supply-points/?postcode=…` (`group_id` `_A` means region `A`)

Prices are pence per kWh including VAT (`value_inc_vat`). Their times are in UTC.

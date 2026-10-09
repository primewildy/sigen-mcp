# /// script
# requires-python = ">=3.11"
# dependencies = ["sigen>=3.0.3,<4", "mcp>=2.3,<3", "aiohttp>=3.9"]
# ///
"""Read-only MCP server for Sigenergy (Sigen) solar + battery systems.

Reads live data and history from Sigen's cloud using the same login as the mySigen app
(via the unofficial `sigen` library). If you tell it your electricity tariff, it also works
out what each day cost or earned. Configuration is by environment variables or an env file;
see README.md and env.example.
"""

import asyncio
import os
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from mcp.server.mcpserver import MCPServer
from sigen import Sigen

ENV_FILE = Path(os.getenv("SIGEN_MCP_ENV_FILE", "~/.config/sigen-mcp/env")).expanduser()
OCTOPUS_API = "https://api.octopus.energy/v1"
STEP_HOURS = 5 / 60  # history readings are 5-minute averages in kW
MAX_RANGE_DAYS = 62

mcp = MCPServer("sigen")
_client: Sigen | None = None
_station: dict = {}
_lock = asyncio.Lock()


def _load_env() -> None:
    """Fill in settings from the env file; real environment variables take precedence."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()


async def _sigen() -> Sigen:
    global _client, _station
    async with _lock:
        if _client is None:
            username = os.getenv("SIGEN_USERNAME")
            password = os.getenv("SIGEN_PASSWORD")
            if not username or not password:
                raise RuntimeError(f"Set SIGEN_USERNAME and SIGEN_PASSWORD (environment or {ENV_FILE})")
            client = Sigen(username=username, password=password, region=os.getenv("SIGEN_REGION", "eu"))
            await client.async_initialize()
            _station = await client.fetch_station_info()
            _client = client
        return _client


async def _timezone() -> ZoneInfo:
    await _sigen()
    return ZoneInfo(os.getenv("SIGEN_TIMEZONE") or _station.get("timeZoneName") or "UTC")


def _currency() -> str:
    return os.getenv("CURRENCY_SYMBOL") or _station.get("currencyCode") or ""


def _parse_date(value: str) -> Date:
    return datetime.strptime(value.replace("-", ""), "%Y%m%d").date()


def _local_midnight(day: Date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz)


async def _fetch_history(day: Date) -> dict:
    client = await _sigen()
    await client.ensure_valid_token()
    ymd = day.strftime("%Y%m%d")
    params = {"dateFlag": "1", "startDate": ymd, "endDate": ymd, "stationId": client.station_id, "fulfill": "false"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{client.BASE_URL}data-process/sigen/station/statistics/energy",
                               headers=client._authenticated_headers(), params=params) as response:
            body = await response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Sigen API error for {ymd}: {body.get('msg')}")
    return body["data"]


# --- Tariffs ---------------------------------------------------------------
# A tariff has three sides: import and export (price per kWh) and standing (price per day),
# all in minor currency units (pence, cents). Each side is a function of (utc, local) time,
# or None when it isn't configured, in which case the money it would affect is reported as null.

def _parse_schedule(value: str) -> list:
    """'24.5' (flat) or '00:00=24.29,02:00=14.58,05:00=24.29' (time of use) -> [(minute_of_day, price)]."""
    value = value.strip()
    if "=" not in value:
        return [(0, float(value))]
    slots = []
    for part in value.split(","):
        clock, price = part.split("=")
        hours, minutes = map(int, clock.strip().split(":"))
        slots.append((hours * 60 + minutes, float(price)))
    slots.sort()
    if slots[0][0] != 0:  # before the first time listed, the last price of the day still applies
        slots.insert(0, (0, slots[-1][1]))
    return slots


def _fixed_side(env_key: str):
    value = os.getenv(env_key)
    if not value:
        return None
    slots = _parse_schedule(value)
    return lambda utc, local: next(p for start, p in reversed(slots) if start <= local.hour * 60 + local.minute)


async def _octopus_get_all(session: aiohttp.ClientSession, url: str, params: dict | None) -> list:
    results = []
    while url:
        async with session.get(url, params=params) as response:
            response.raise_for_status()
            body = await response.json()
        results.extend(body.get("results", []))
        url, params = body.get("next"), None
    return results


async def _octopus_region(session: aiohttp.ClientSession) -> str:
    if os.getenv("OCTOPUS_REGION"):
        return os.environ["OCTOPUS_REGION"].strip().lstrip("_").upper()
    postcode = os.getenv("OCTOPUS_POSTCODE")
    if not postcode:
        raise RuntimeError("Set OCTOPUS_REGION (A–P) or OCTOPUS_POSTCODE for Octopus prices")
    rows = await _octopus_get_all(session, f"{OCTOPUS_API}/industry/grid-supply-points/",
                                  {"postcode": postcode.replace(" ", "")})
    if not rows:
        raise RuntimeError(f"Octopus has no region for postcode {postcode}")
    return rows[0]["group_id"].lstrip("_")


def _octopus_side(rows: list):
    def ts(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    periods = sorted(((ts(r["valid_from"]), ts(r["valid_to"]), r["value_inc_vat"]) for r in rows),
                     key=lambda p: p[0])

    def price(utc, local):
        for start, end, value in reversed(periods):
            if start <= utc and (end is None or utc < end):
                return value
        raise RuntimeError(f"No Octopus price covers {utc.isoformat()}")
    return price


async def _octopus_tariff(first: Date, last: Date, tz: ZoneInfo) -> tuple[dict, dict]:
    import_product = os.getenv("OCTOPUS_IMPORT_PRODUCT")
    export_product = os.getenv("OCTOPUS_EXPORT_PRODUCT")
    if not import_product and not export_product:
        raise RuntimeError("TARIFF_SOURCE=octopus needs OCTOPUS_IMPORT_PRODUCT and/or OCTOPUS_EXPORT_PRODUCT")

    def utc_z(moment):
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    params = {"period_from": utc_z(_local_midnight(first, tz)),
              "period_to": utc_z(_local_midnight(last + timedelta(days=1), tz)), "page_size": 1500}

    def base(product):
        return f"{OCTOPUS_API}/products/{product}/electricity-tariffs/E-1R-{product}-{region}"

    async with aiohttp.ClientSession() as session:
        region = await _octopus_region(session)
        wanted = {}
        if import_product:
            wanted["import"] = f"{base(import_product)}/standard-unit-rates/"
            wanted["standing"] = f"{base(import_product)}/standing-charges/"
        if export_product:
            wanted["export"] = f"{base(export_product)}/standard-unit-rates/"
        rows = await asyncio.gather(*(_octopus_get_all(session, url, dict(params)) for url in wanted.values()))

    sides = {"import": None, "export": None, "standing": None}
    sides.update({side: _octopus_side(r) for side, r in zip(wanted, rows)})
    labels = {"import": f"Octopus {import_product} (region {region})" if import_product else "not configured",
              "export": f"Octopus {export_product} (region {region})" if export_product else "not configured"}
    return sides, labels


async def _load_tariff(first: Date, last: Date, tz: ZoneInfo) -> dict:
    source = os.getenv("TARIFF_SOURCE", "none").strip().lower()
    if source == "octopus":
        current, labels = await _octopus_tariff(first, last, tz)
    elif source == "fixed":
        current = {"import": _fixed_side("IMPORT_RATE"), "export": _fixed_side("EXPORT_RATE"),
                   "standing": _fixed_side("STANDING_CHARGE")}
        labels = {side: f"fixed {os.getenv(key)}" if os.getenv(key) else "not configured"
                  for side, key in (("import", "IMPORT_RATE"), ("export", "EXPORT_RATE"))}
    elif source == "none":
        current, labels = {"import": None, "export": None, "standing": None}, \
                          {"import": "not configured", "export": "not configured"}
    else:
        raise RuntimeError(f"Unknown TARIFF_SOURCE {source!r}: use octopus, fixed or none")

    previous = {"import": _fixed_side("PREVIOUS_IMPORT_RATE"), "export": _fixed_side("PREVIOUS_EXPORT_RATE"),
                "standing": _fixed_side("PREVIOUS_STANDING_CHARGE")}
    starts = {side: _parse_date(os.environ[key]) if os.getenv(key) else None
              for side, key in (("import", "TARIFF_IMPORT_FROM"), ("export", "TARIFF_EXPORT_FROM"))}
    starts["standing"] = starts["import"]
    return {"current": current, "previous": previous, "starts": starts, "labels": labels, "tz": tz}


def _tariff_on(tariff: dict, day: Date) -> tuple[dict, dict]:
    """The price functions that apply on `day`, and a label for each of import/export."""
    sides, labels = {}, {}
    for side in ("import", "export", "standing"):
        start = tariff["starts"][side]
        on_current = start is None or day >= start
        sides[side] = tariff["current" if on_current else "previous"][side]
        if side != "standing":
            labels[side] = (tariff["labels"][side] if on_current
                            else os.getenv(f"PREVIOUS_{side.upper()}_RATE") and "previous rate"
                            or f"unknown (before {start.isoformat()})")
    return sides, labels


def _rate_bands(sides: dict, day: Date, tz: ZoneInfo) -> list:
    """The day's price bands in local time, labelled cheap/day/peak by import (else export) price."""
    if sides["import"] is None and sides["export"] is None:
        return []
    start = _local_midnight(day, tz).astimezone(timezone.utc)
    bands = []
    for slot in range(50):  # 30-minute steps; a clock-change day can have 50
        utc = start + timedelta(minutes=30 * slot)
        local = utc.astimezone(tz)
        if local.date() != day:
            break
        prices = tuple(round(sides[s](utc, local), 2) if sides[s] else None for s in ("import", "export"))
        if bands and (bands[-1]["import_rate"], bands[-1]["export_rate"]) == prices:
            continue
        if bands:
            bands[-1]["to"] = local.strftime("%H:%M")
        bands.append({"from": local.strftime("%H:%M"), "to": None, "import_rate": prices[0], "export_rate": prices[1]})
    bands[-1]["to"] = "24:00"
    key = "import_rate" if sides["import"] else "export_rate"
    levels = sorted({b[key] for b in bands})
    for band in bands:
        band["band"] = ("day" if len(levels) == 1 else "cheap" if band[key] == levels[0]
                        else "peak" if band[key] == levels[-1] else "day")
    return bands


# --- Day summary -----------------------------------------------------------

def _summarise(day: Date, history: dict, tariff: dict) -> dict:
    tz = tariff["tz"]
    sides, labels = _tariff_on(tariff, day)
    known_import, known_export = sides["import"] is not None, sides["export"] is not None
    bands = _rate_bands(sides, day, tz)
    items = history["itemList"]

    totals = dict.fromkeys(("solar", "house_use", "house_import", "battery_grid_charge", "battery_export",
                            "solar_export", "battery_in", "battery_out"), 0.0)
    by_band = {}
    import_cost = export_value = no_system_cost = 0.0
    grid_charge_times = []
    full_at = empty_at = prev_soc = None

    for item in items:
        local = datetime.strptime(item["dataTime"], "%Y%m%d %H:%M").replace(tzinfo=tz)
        utc = local.astimezone(timezone.utc)
        clock = local.strftime("%H:%M")
        import_p = sides["import"](utc, local) if known_import else None
        export_p = sides["export"](utc, local) if known_export else None

        battery = item["esChargeDischargePower"]  # battery total: + charging / - discharging
        grid_charge = max(0.0, min(max(0.0, battery), item["fromGridPower"] - item["loadPower"]))
        kwh = {k: v * STEP_HOURS for k, v in {
            "solar": item["pvTotalPower"], "house_use": item["loadPower"],
            "house_import": item["fromGridPower"] - grid_charge, "battery_grid_charge": grid_charge,
            "battery_export": max(0.0, -battery - item["esDischargePower"]), "solar_export": item["toGridPower"],
            "battery_in": max(0.0, battery), "battery_out": max(0.0, -battery)}.items()}
        for key, value in kwh.items():
            totals[key] += value
        imported = kwh["house_import"] + kwh["battery_grid_charge"]
        exported = kwh["battery_export"] + kwh["solar_export"]

        if bands:
            name = next(b["band"] for b in reversed(bands) if b["from"] <= clock)
            b = by_band.setdefault(name, dict.fromkeys(
                ("house_import_kwh", "battery_grid_charge_kwh", "export_kwh", "import_cost", "export_value"), 0.0))
            b["house_import_kwh"] += kwh["house_import"]
            b["battery_grid_charge_kwh"] += kwh["battery_grid_charge"]
            b["export_kwh"] += exported
            if known_import:
                b["import_cost"] -= imported * import_p / 100
            if known_export:
                b["export_value"] += exported * export_p / 100
        if known_import:
            import_cost += imported * import_p
            no_system_cost += kwh["house_use"] * import_p
        if known_export:
            export_value += exported * export_p

        soc = item["batSoc"]
        if grid_charge > 0.1:
            grid_charge_times.append(clock)
        if full_at is None and soc >= 99.5:
            full_at = clock
        if prev_soc is not None and prev_soc > 0.5 and soc <= 0.5:
            empty_at = clock
        prev_soc = soc

    midnight = _local_midnight(day, tz)
    standing = sides["standing"](midnight.astimezone(timezone.utc), midnight) if sides["standing"] else None
    socs = [i["batSoc"] for i in items]
    soc_after_charge = (max(i["batSoc"] for i in items if i["dataTime"][9:] <= grid_charge_times[-1])
                        if grid_charge_times else None)

    def money(value, *needs):
        return round(value / 100, 2) if all(needs) else None

    r = lambda v: round(v, 2)
    return {
        "date": day.isoformat(),
        "energy_kwh": {
            "solar": r(totals["solar"]), "house_use": r(totals["house_use"]),
            "grid_import_total": r(totals["house_import"] + totals["battery_grid_charge"]),
            "grid_import_for_house": r(totals["house_import"]),
            "grid_import_to_charge_battery": r(totals["battery_grid_charge"]),
            "export_total": r(totals["battery_export"] + totals["solar_export"]),
            "export_from_battery": r(totals["battery_export"]), "export_from_solar": r(totals["solar_export"]),
            "battery_charged": r(totals["battery_in"]), "battery_discharged": r(totals["battery_out"]),
        },
        "battery": {
            "soc_start_pct": socs[0] if socs else None, "soc_end_pct": socs[-1] if socs else None,
            "soc_max_pct": max(socs) if socs else None,
            "grid_charge_window": [grid_charge_times[0], grid_charge_times[-1]] if grid_charge_times else None,
            "soc_after_grid_charge_pct": soc_after_charge,
            "first_full_at": full_at, "last_emptied_at": empty_at,
        },
        "tariff": labels,
        "money": {
            "currency": _currency(),
            "import_cost": money(-import_cost, known_import),
            "export_value": money(export_value, known_export),
            "standing_charge": money(-(standing or 0), standing is not None),
            "net": money(export_value - import_cost - (standing or 0), known_import, known_export, standing is not None),
            "without_solar_or_battery": money(-(no_system_cost + (standing or 0)), known_import, standing is not None),
            "saved": money(export_value - import_cost + no_system_cost, known_import, known_export),
        },
        "by_rate_band": {name: {k: (None if (k == "import_cost" and not known_import)
                                       or (k == "export_value" and not known_export) else r(v))
                                for k, v in vals.items()} for name, vals in by_band.items()},
        "rate_bands": bands,
        "notes": "Money is in major currency units (negative = paid out), from the configured tariff; null means "
                 "a price isn't configured for that day. Estimates: suppliers bill from the meter. Today's "
                 "summary only covers readings so far.",
    }


async def _day_summary(day: Date, tariff: dict | None = None) -> dict:
    tariff = tariff or await _load_tariff(day, day, await _timezone())
    return _summarise(day, await _fetch_history(day), tariff)


# --- Tools -----------------------------------------------------------------

@mcp.tool()
async def station_info() -> dict:
    """Static info about the system: station ID, PV capacity (kW), battery capacity (kWh), grid/EV flags,
    time zone and currency."""
    return await (await _sigen()).fetch_station_info()


@mcp.tool()
async def energy_flow() -> dict:
    """Live snapshot: PV power, house load, battery power (+charging / -discharging), battery SOC %,
    grid buySellPower (negative = importing from grid, positive = exporting), plus today's PV energy (kWh). Powers are in kW."""
    return await (await _sigen()).get_energy_flow()


@mcp.tool()
async def operating_mode() -> dict:
    """The battery's current operating mode (e.g. Sigen AI, Maximum Self-Powered, TOU) and the modes available."""
    client = await _sigen()
    current = await client.get_operational_mode()
    modes = await client.get_operational_modes()
    return {"current": current, "available": modes}


@mcp.tool()
async def energy_history(date: str) -> dict:
    """Raw history for one day (YYYYMMDD): Sigen's daily totals plus 5-minute readings in `itemList`
    (kW; batSoc %). Prefer day_summary unless you need the raw curve. Field meanings, checked against the app:
    - esChargeDischargePower: the battery's TOTAL power (+charging / -discharging), including battery->grid export.
    - esDischargePower: only the battery->house part; toGridPower: only solar->grid.
    - Battery->grid export = max(0, -esChargeDischargePower - esDischargePower).
    - Sigen's daily powerFromGrid leaves out grid->battery charging; integrate fromGridPower instead."""
    return await _fetch_history(_parse_date(date))


@mcp.tool()
async def tariff_rates(date: str) -> dict:
    """The electricity prices configured for one day (YYYYMMDD): import and export price bands in local time
    (minor units per kWh, e.g. pence, labelled cheap/day/peak), the daily standing charge, and where the
    prices come from. Empty if no tariff is configured."""
    day = _parse_date(date)
    tz = await _timezone()
    tariff = await _load_tariff(day, day, tz)
    sides, labels = _tariff_on(tariff, day)
    midnight = _local_midnight(day, tz)
    standing = sides["standing"](midnight.astimezone(timezone.utc), midnight) if sides["standing"] else None
    return {"date": day.isoformat(), "currency": _currency(), "source": labels,
            "bands": _rate_bands(sides, day, tz),
            "standing_charge_per_day": round(standing, 2) if standing is not None else None}


@mcp.tool()
async def day_summary(date: str) -> dict:
    """Everything that happened on one day (YYYYMMDD), worked out correctly from the 5-minute data:
    solar, house use, grid import split into house vs battery charging, export split into battery vs solar,
    battery state (grid-charge window, % reached, when full/empty) and, if a tariff is configured, money
    (import cost, export value, standing charge, net, cost without solar or battery, saved) with a
    breakdown by cheap/day/peak price band."""
    return await _day_summary(_parse_date(date))


@mcp.tool()
async def date_range_summary(start: str, end: str) -> dict:
    """Totals over a range of days (YYYYMMDD to YYYYMMDD inclusive, max 62 days): energy and money summed,
    plus a one-line row per day (solar, grid import, export, net, saved, % reached on the grid charge).
    Money totals are null if any day in the range has no price configured; see days_with_unknown_prices."""
    first, last = _parse_date(start), _parse_date(end)
    if last < first:
        raise ValueError("end is before start")
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"Range is longer than {MAX_RANGE_DAYS} days; split it up")
    tz = await _timezone()
    today = datetime.now(tz).date()
    last = min(last, today)
    tariff = await _load_tariff(first, last, tz)
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    summaries = []
    for chunk in range(0, len(days), 5):  # be gentle with the Sigen API
        summaries += await asyncio.gather(*(_day_summary(d, tariff) for d in days[chunk:chunk + 5]))

    def total(section, key):
        values = [s[section][key] for s in summaries]
        return None if None in values else round(sum(values), 2)

    money_keys = [k for k in summaries[0]["money"] if k != "currency"]
    return {
        "from": first.isoformat(), "to": last.isoformat(), "days": len(summaries),
        "includes_partial_today": last == today,
        "energy_kwh": {k: total("energy_kwh", k) for k in summaries[0]["energy_kwh"]},
        "money": {"currency": _currency(), **{k: total("money", k) for k in money_keys}},
        "days_with_unknown_prices": [s["date"] for s in summaries if None in s["money"].values()],
        "daily": [{
            "date": s["date"], "solar_kwh": s["energy_kwh"]["solar"],
            "grid_import_kwh": s["energy_kwh"]["grid_import_total"], "export_kwh": s["energy_kwh"]["export_total"],
            "grid_charge_to_pct": s["battery"]["soc_after_grid_charge_pct"],
            "net": s["money"]["net"], "saved": s["money"]["saved"],
        } for s in summaries],
    }


@mcp.tool()
async def smart_loads() -> list:
    """Smart loads connected to the system (e.g. immersion heater) with their state and power."""
    return await (await _sigen()).get_smart_loads()


if __name__ == "__main__":
    mcp.run()

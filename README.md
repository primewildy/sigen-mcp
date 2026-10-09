# sigen-mcp

A read-only [MCP](https://modelcontextprotocol.io) server for **Sigenergy (Sigen)** solar and battery
systems. Ask your AI assistant what your battery is doing, what happened yesterday, or how much the
system saved you last month.

It logs in to Sigen's cloud with your mySigen app account, using the unofficial
[`sigen`](https://pypi.org/project/sigen/) library. It can't change any settings.

> **Unofficial.** Not affiliated with Sigenergy. It uses the same private API as the mySigen app,
> which can change without notice.

## Tools

| Tool | What it gives you |
|---|---|
| `energy_flow` | Live: solar, house load, battery power and charge %, grid import/export |
| `station_info` | System details: solar and battery size, time zone, currency |
| `operating_mode` | Current battery mode (Sigen AI, Self-Powered, TOU, …) and the modes available |
| `smart_loads` | Smart loads (e.g. an immersion heater) and their state |
| `day_summary` | One day, worked out properly: solar, house use, grid import (house vs charging the battery), export (battery vs solar), when the battery charged, filled and emptied, and money if a tariff is set |
| `date_range_summary` | The same totals over up to 62 days, with a row per day |
| `tariff_rates` | The prices configured for a day, in price bands |
| `energy_history` | Raw 5-minute readings for one day |

## Install

Needs Python 3.11+.

With [uv](https://docs.astral.sh/uv/) there's nothing to install: `uv run server.py` reads the
dependencies from the top of the file.

Or with a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configure

```bash
mkdir -p ~/.config/sigen-mcp
cp env.example ~/.config/sigen-mcp/env
chmod 600 ~/.config/sigen-mcp/env
```

Then fill in your mySigen login. That's enough for live data and energy history.

To get money figures too, set a tariff in the same file:

- **`TARIFF_SOURCE=fixed`**: your import, export and standing-charge prices. They can be one flat
  price or a time-of-use schedule like `02:00=14.58,05:00=24.29,16:00=34.01,19:00=24.29`.
- **`TARIFF_SOURCE=octopus`** (UK): prices come live from Octopus's public API. Set your product
  codes and your region letter or postcode.
- If you changed tariff recently, set `TARIFF_IMPORT_FROM` / `TARIFF_EXPORT_FROM`. Earlier days
  then use the `PREVIOUS_*` prices, or show as unknown.

All prices are in pence or cents per kWh. Money is reported in pounds, euros or dollars.

## Add it to your MCP client

Claude Code:

```bash
claude mcp add sigen -s user -- uv run /path/to/sigen-mcp/server.py
# or, with the virtual environment:
claude mcp add sigen -s user -- /path/to/sigen-mcp/.venv/bin/python /path/to/sigen-mcp/server.py
```

Claude Desktop and other clients (`mcpServers` in their JSON config):

```json
"sigen": {
  "command": "uv",
  "args": ["run", "/path/to/sigen-mcp/server.py"]
}
```

You can put settings in that config's `env` block instead of the env file. Settings there take
precedence over the file.

## How the numbers are worked out

Sigen's history comes as 5-minute average readings. Some fields aren't what their names suggest, so
the summaries use these rules (checked against the mySigen app):

- `esChargeDischargePower` is the battery's total power (+ charging, − discharging), including what
  it sells to the grid. `esDischargePower` is only the part going to the house.
- Battery export = `max(0, −esChargeDischargePower − esDischargePower)`. `toGridPower` is solar
  export only.
- Sigen's own daily `powerFromGrid` leaves out grid charging of the battery, so grid import is added
  up from the 5-minute `fromGridPower` instead.
- In the live `energy_flow`, a negative `buySellPower` means importing.

Money figures are estimates from the prices you configure. Your supplier bills from your meter.

Full endpoint and field notes are in [docs/sigen-api.md](docs/sigen-api.md).

## Your credentials

Your mySigen password stays on your machine, in the env file or your MCP client's config. The server
only sends it to Sigen's login endpoint (encrypted, as the app does). The env file and saved JSON
responses are in `.gitignore`, so a clone of this repo won't pick them up.

## Sources and credits

### Dependencies

These are installed from PyPI. None of their code is copied into this repo.

| Package | Used for | Licence |
|---|---|---|
| [`sigen`](https://pypi.org/project/sigen/) (GitHub user fbradyirl) | Sigen cloud login, live flow, modes and smart loads | MIT (per its PyPI classifier and bundled licence file). Its GitHub repo, `fbradyirl/sigen`, was not reachable at the time of writing. |
| [`mcp`](https://github.com/modelcontextprotocol/python-sdk) (MCP Python SDK) | The MCP server | MIT |
| [`aiohttp`](https://github.com/aio-libs/aiohttp) | HTTP requests | Apache-2.0 (with some MIT parts) |
| [`pycryptodome`](https://github.com/Legrandin/pycryptodome) (installed by `sigen`) | Password encryption at login | BSD 2-Clause and public domain |

### Reference material

No code was copied from these. They were used for facts about the APIs, which this repo
reimplements independently.

| Source | What it gave us | Licence |
|---|---|---|
| [GerardBrowne/sig-data](https://github.com/GerardBrowne/sig-data) | The path and parameter names of the day-history endpoint (`data-process/sigen/station/statistics/energy`) | No licence published |
| [paulczar/sigen-mcp](https://github.com/paulczar/sigen-mcp) (`docs/sigencloud-api.md`) | Background on Sigen's end-user and developer APIs | MIT |
| [Octopus Energy public API](https://developer.octopus.energy/) | Tariff unit rates, standing charges and postcode-to-region lookup | Octopus's API terms |

The meaning of each history field, in [docs/sigen-api.md](docs/sigen-api.md), was worked out by
comparing the API data with the mySigen app.

"Sigenergy", "Sigen" and "mySigen" are trademarks of Sigenergy Technology Co., Ltd. "Octopus
Energy" and "Flux" belong to Octopus Energy Group. This project isn't affiliated with or endorsed
by either.

## Licence

MIT. See [LICENSE](LICENSE). Dependencies keep their own licences, listed above.

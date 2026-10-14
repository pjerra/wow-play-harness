# wow-play-harness

A headless / scripted WoW 1.12 (Turtle WoW 1.18.1) play harness, used to test server-side bot modules
(it drove [TortoiseBots](https://github.com/Sagiroth/TortoiseBots) regression checks). Python package `wowharness`.
It is published as an **example**: study it, borrow from it, adapt it. It is not a supported product.
It was the predecessor of [turtle-agent](https://github.com/pjerra/turtle-agent), which now owns the
headless login; `wowharness/headless.py` and `mcp_headless.py` talk to turtle-agent over JSON-RPC.

It can drive a real Windows client (window handles, key/mouse posts, chat log tail, screenshots) or the
headless backend, send GM/bot commands over the core's SOAP interface, parse the combat log, walk a
character over server navmeshes, and run scripted regression scenarios ("build gate") with reports.

## Layout

| Folder | Contents |
| :--- | :--- |
| `wowharness/` | The package: client/window/keys (Windows client driver), server (SOAP, ssh logs), combat + timeline parsers, navmesh/navwalk (Detour via ctypes, pure-Python fallback), buildgate scenarios, MCP servers (`mcp.py`, `mcp_headless.py`), dashboard client |
| `addon/HarnessObserver/` | In-game Lua addon that prints `HX|...` state lines the harness reads |
| `native/` | C++ glue (`navdetour.cpp`) and vendored Detour (zlib licence); see `native/README.md` |
| `tools/` | CLI scripts: `buildgate.py`, `compare_runs.py`, `run_timeline.py`, `headless_mvp.py`, `build_navdetour.sh` |
| `tests/` | pytest suite (pure Python, fixtures are recorded server samples) |
| `docs/` | Design spec, MVP plan, timeline tools notes (historical; host names are anonymised) |

## Requirements

Python >= 3.11; `mcp<2`, `requests`, `Pillow`, `pywin32` (Windows only, for the real-client driver).
A Turtle WoW 1.18.1 server with SOAP enabled and a client you own are needed for live use; no game data
is included in this repo. The client driving part only works on Windows.

## Install

    python3 -m venv .venv && . .venv/bin/activate
    pip install -e . pytest

## Configure

Copy `config.example.json` to `~/.wowharness.json` (or point `WOWHARNESS_CONFIG` at it) and fill in your
client folder, server host, SOAP GM account and a human game account. Keep it out of git (it is in
`.gitignore`). `.env.example` lists the optional environment variables.

## Run

    python tools/buildgate.py --config tools/buildgate_uldaman.json --dry-run
    python -m wowharness.mcp_headless --help     # MCP server for use from an AI agent

## Test

    python -m pytest -q

Network/real-data tests skip themselves (set `WOWHARNESS_MMAPS` to a folder of server mmap tiles to enable the navmesh one).

## Licence

See `LICENSE` (placeholder until the owner picks one). `native/detour/` stays under zlib.

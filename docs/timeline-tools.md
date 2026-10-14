# Timeline tools

- **Live**: MCP tool `fight_timeline` (`wowharness/timeline.py: record_fight`) samples the dashboard, the player and
  nearby units for N seconds, sends an optional pull order, reads turtle-agent's combat/chat events of the window and
  writes `timeline-<label>.md/.json` (one row per second per bot, plus the annotations).
- **Offline**: `python tools/run_timeline.py <run dir> [--names A,B] [--from HH:MM:SS --to HH:MM:SS] [--out timeline.md]`
  (`wowharness/offline_timeline.py`) merges a finished test directory into one time-ordered view.
  - Reads `botwatch.jsonl` (`{"t": unix, "bots": [dashboard bot dicts]}`, written by TortoiseBots-audit
    `tools/botwatch.py`) and the agent events (`events-final.json`, `events.jsonl`, `events.json`; raw lists, `events`
    RPC pages or JSON-RPC replies; several files are merged and de-duplicated by `seq`). Both go onto epoch seconds;
    `--from/--to` are HH:MM:SS on the events' own UTC offset.
  - Prints the annotations of the whole window first (standstill out of range, CC starts vs completions, first-heal
    latency, deaths; the same code as the live timeline, `timeline.annotate`), then one line per spell start/go/fail,
    aura applied/removed, melee, spell damage, heal, death, kill and chat/system event that involves a tracked bot (or
    a Polymorph / chat / death), each with the nearest dashboard sample (within 6 s) of the bots involved: state,
    `last_action`, `last_trigger`, hp %, and the distance between source and target when both are bots (else from the
    bot to its dashboard `target` when that is a bot). Mobs have no dashboard position, so distances to mobs are unknown.
  - `--names` defaults to the bots in `botwatch.jsonl`, else to the player-guid sources/targets of party-flagged events.
    Without `botwatch.jsonl` only the agent side is printed. Exit code 2 and a message on a missing events file or bad time.
- **Compare**: `tools/compare_runs.py` diffs saved build gate results and `timeline-*.json` between builds.

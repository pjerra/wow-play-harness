# WoW play harness — design

Date: 2026-10-11. Status: approved in conversation, awaiting spec review.

## Purpose

Let Claude Code play a real WoW 1.18.1 client (Turtle-style vanilla 1.12) against a private
TortoiseBots test server: log a human character in, group with bots, fight, read what happened,
and turn it into bug reports and fix plans for TortoiseBots (github.com/Sagiroth/TortoiseBots).

## What the owner asked for (given)

- Input by window handle, never to the focused window. Most actions through the chat box
  (`/script <lua>`, `/tar`, `/cast`, `.bot ...`); movement by timed key holds.
- Observe through `/combatlog` and `/chatlog` files, a small observer addon that prints state
  lines to chat, screenshots, and the server side (GM commands over SOAP, server logs).
- Expose it all as an MCP server to Claude Code.
- Scenarios and reports later; an MVP first.
- Known traps: `WoW.exe` is the game (`TurtleWoW.exe` installer, `turtle-wow.exe` launcher);
  `gxWindow "1"`, `movie "0"`; ESC during the intro quits; TAB does not move from account to
  password, click each box; check the client is alive before every capture; use a human-owned
  account, never a bot's; close or minimise OS popups.

## Decisions (made in conversation)

| Question | Decision |
|---|---|
| Server | The lab server `game-server` (Tailscale 192.0.2.10), which the client's realmlist already names |
| How the server gets there | Yu'lon installs `wow-tortoise` (Penqle core 1181dev + SOAP, TortoiseBots compiled in statically) |
| Where the code lives | New private repo `~/wow-play-harness` (GitHub `<owner>/wow-play-harness`, private) |
| Approach | A: a Python MCP server that runs on Windows (`py.exe`), launched from WSL over stdio |

## Assumptions (not yet proven — the spike checks the first three)

1. `PostMessage` of `WM_KEYDOWN`/`WM_CHAR` to the WoW window reaches the chat box while the window
   is not focused.
2. `/combatlog` writes `Logs\WoWCombatLog.txt` and `/chatlog` writes `Logs\WoWChatLog.txt` while
   playing (not only on logout), flushed often enough to tail.
3. The combat log uses vanilla string formats (`CHAT_MSG_SPELL_*`, `CHAT_MSG_COMBAT_*` text), not
   `COMBAT_LOG_EVENT_UNFILTERED`.
4. The Yu'lon `wow-tortoise` install creates the human account `player/player`.
5. WSL starts `py.exe` in the logged-in desktop session (no session-0 problem), so windows and
   input work.

## Components

Code is edited on the Linux side (`~/wow-play-harness`) and synced to
`C:\Users\<you>\wow-play-harness` to run; nothing builds or tests under `/mnt/c`.

```
WSL: Claude Code ──stdio──▶ py.exe -m wowharness.mcp            (Windows)
  wowharness/
    client.py   launch WoW.exe, find its HWND by pid, alive check, Config.wtf (gxWindow, movie, realmlist)
    keys.py     PostMessage key down/up and WM_CHAR to the HWND; chat(text); key(name, hold_ms); click(x, y)
    logs.py     tail WoWCombatLog.txt / WoWChatLog.txt by byte offset; handles truncation and rotation
    combat.py   parse 1.12 combat-log lines into events (time, source, action, spell, target, amount, kind)
    report.py   group events by actor; summary text per actor
    server.py   SOAP to gameserver for .bot and GM commands; ssh `docker logs` for the server log
    shot.py     PNG of the WoW window (PrintWindow)
    mcp.py      the MCP tool surface
  addon/HarnessObserver/   1.12 Lua addon: prints `HX|...` state lines to chat (→ chat log)
```

Each module has one job and a small interface; the Win32 and SOAP edges sit behind interfaces so
tests can swap in fakes.

### MCP tools (MVP)

| Tool | Does |
|---|---|
| `launch()` | Write Config.wtf keys, start `WoW.exe`, wait for its window |
| `login(account, password, char)` | Click account box, type; click password box, type; Enter; Enter at character select; enable `/chatlog` and `/combatlog`; done when `HX|ready` is in the chat log |
| `chat(text)` | Enter, text, Enter |
| `run_lua(code)` | `chat("/script " + code)` |
| `key(name, hold_ms)` | Key down, wait, key up |
| `screenshot()` | PNG path of the window |
| `state()` | Ask the addon for one state line and return it parsed |
| `tail_combat_log(since)` / `tail_chat(since)` | New lines since a byte offset, plus the new offset |
| `bot(cmd)` | `chat(".bot " + cmd)` — player commands per TortoiseBots `docs/guides/player-controls.md` |
| `gm(cmd)` | Console command over SOAP, reply verbatim |
| `fight_report(since)` | Parsed combat log since an offset, per actor, plus bot whispers and `TBM:` lines; JSON and a short text summary |

### Observer addon

Prints compact lines to chat, e.g. `HX|p=Name,hp%,mp%,x,y,zone|t=Name,hp%|party=Bot1:hp%,Bot2:hp%|cast=Spell`.
On `/hx` and on a 1 s timer while `/hx on`. `HX|ready` once on login. TBM protocol lines are read
from the chat log as TortoiseBots already sends them.

## MVP flow

1. `launch()`.
2. `login("player", "player", "<char>")` — one pre-made level 10+ character on the human account.
3. `bot("hire warrior tank")`, `bot("hire priest healer")`; done when the `TBM:` replies and party
   joins show in the chat log.
4. `run_lua("TargetNearestEnemy()")`, `run_lua("AttackTarget()")`; the addon prints target HP each
   second until it dies or 90 s pass.
5. `fight_report(offset taken before the pull)` returns the parsed fight and "what the bots did".

## Errors

- Before every action: process alive and window present; a dead client answers
  `client not running (exit code N)`, never an empty answer.
- Every wait has a timeout; a timeout answers with the reason, the log tails and a screenshot path.
- No focus needed (input by handle); a minimised window is restored without activating it.
- Log files that do not grow after `/combatlog` / `/chatlog`: say so ("combat log not live").
- SOAP errors are returned verbatim.

## Testing

- Unit tests (pytest, on Linux, no client): combat-log parser on recorded 1.12 lines, log tailer
  offsets/truncation, key encoding, report builder.
- Live checks by hand against the real client and server; evidence (log excerpts, screenshots)
  saved under `docs/evidence/`. Nothing is called tested in game unless it ran there.

## Order of work

1. Spike (~15 min, throwaway, findings into `docs/evidence/spike.md`): assumptions 1–3. If
   `PostMessage` fails, stop and redesign with the owner.
2. Server: Yu'lon installs `wow-tortoise` on gameserver; check: realm answers on 3724, `.bot version`
   over SOAP.
3. Driver: `launch`, `login`, `chat`, `run_lua`, `key`, `screenshot`, alive check.
4. Observer addon and log tailers.
5. `bot`, `gm`.
6. `fight_report`.
7. MVP demo to the owner; stop there.

## Not in the MVP

Scenario files and runner, TortoiseBots metrics/dashboard tap, movement and pathing beyond key
holds, multiple clients, automatic bug-report drafting.

## Addendum 2026-10-11: TortoiseBots dashboard as an observation source

Owner: "Also use the bot dashboard for info." The TortoiseBots observability daemon
(`tools/observability`, docs/guides/observability-dashboard.md) serves per-bot live state that the
combat log cannot show: the AI's decision trail.

- Login: `POST /api/v1/auth/login` `{"username","password"}` (game account, GM rank >= 2) sets cookie
  `tbm_session`; every API call sends it.
- `GET /api/v1/bots`: roster snapshots; per bot `name, guid, class, role, level, hp/max_hp,
  power/max_power, target, strategy, state, last_action, last_trigger, map/zone/x/y/z`.
- `GET /api/v1/issues`, `GET /api/v1/anomalies` (last 1000), `GET /api/v1/anomalies/totals`.
- New module `wowharness/dashboard.py`: `Dashboard(base_url, user, password, session=requests.Session())`
  with `login()`, `bots(names=None)`, `issues()`, `anomalies()`; and a pure
  `decision_timeline(samples, names)` that turns successive snapshots into per-bot change events
  (`last_action`, `last_trigger`, `target`, `state`, hp%).
- `fight_report` gains `decisions` (the timeline sampled about once a second during the fight by a
  new `watch_bots(names, seconds)` tool) and the issues/anomalies seen in the window.
- Config keys: `dashboard_url` (e.g. `http://192.0.2.12:8095`), `dashboard_user`,
  `dashboard_password` (may equal the SOAP GM account). Optional: missing keys disable the tools
  with a clear message.
- Server side: the observability daemon must run and port 8095 must answer from the tailnet
  (asked of the server sessions).

## Addendum 2026-10-12: live pixel channel (no ReloadUI)

Owner: testers must read chat/combat/state from the running client without ReloadUI (5 s, interrupts
casts, clears the target). The observer addon now also paints its lines into a strip at the top-left
of the screen, and `wowharness/pixel.py` reads them off the window.

- Addon: 160 x 3 cells of 3 screen units (3 px at 1024x768; 5 x 4.6 px when a 1024x768 back buffer
  is stretched to 1707x1067), strata TOOLTIP, parentless (Alt-Z does not hide it). 6 bits per cell
  (4 levels per channel). A page = 6 sync cells (pitch + level calibration), row markers in column 0
  (vertical pitch), a 19-byte header (magic, cols/rows, flags, session, seq, stream offset, length,
  drop count, Fletcher-16) and up to 335 payload bytes. New page every 0.1 s while lines wait, an
  empty heartbeat page after 1 s idle. `/hx pix off|on`. Helpers: `HX_Mark(text)`, `HX_Fps()`,
  `HX_PixTest(n, len)`.
- Capture: BitBlt from the game window's own DC. Feasibility checked live: it returns client 2's
  frame while client 1 (foreground) fully covers it at the same screen rect; PrintWindow
  (PW_RENDERFULLCONTENT) works too but is slower (30 vs 12 ms full frame); a screen capture returns
  whatever covers the window, so it is not used. A minimised window renders nothing.
- Harness: `PixelReader` thread (~30 captures/s of a 1024x48 region), decodes, checks the checksum,
  drops repeats by sequence number, counts gaps (missed pages) and addon-side drops, and keeps lines
  in memory. `Client.live_offset() / live_lines(since, kind) / wait_live(pattern, since, timeout)`;
  MCP `live_log(since, kind, pattern, timeout_s)`; `state()` reads live and falls back to the
  ReloadUI path when no strip answers within 5 s. The ReloadUI tools are unchanged.
- Measured on client 2 (Harnessa, 2026-10-12): 35 lines/s (80-char lines; ~3.6 KB/s) sustained,
  1000 numbered lines with 0 missing, 0 gaps, 0 corrupt pages; a line reaches the harness within
  ~0-50 ms of the Enter that triggered it when nothing is queued; capture 3.2-5 ms, reader thread
  3-4 % of one core; WoW CPU with the strip idle / off / paging: 68.9 / 65.2 / 67.2 % of one core
  over 10 s each (within noise).
- Seen live: a display change made both clients re-create their window (new HWND, 1024x768) and the
  strip was found hidden afterwards (cause not proven: a `/hx pix off` from the CPU test had just run,
  and its `/hx pix on` may have been lost); the addon now re-shows it (logs `HX|pix=reshown`) and `Window.alive()`
  follows the process to its new window.
- Stale capture (builds 12 and 13 on gameserver, client 1, 15:15 and 15:54): the live lines stopped for
  minutes (walk_to: "lost position: 3 .gps reads without a reply") while the game ran on. The reader
  thread was alive with no error and kept decoding the same page (repeats rising); the game's own
  screenshots taken during the freeze (b12_b..e, y_b13_4/5) decode to pages hundreds of sequence
  numbers further on, and when lines came back the stream showed one gap of 22.6 KB. So the
  window-DC BitBlt kept returning an old frame (the DWM redirection surface stopped updating; the
  exact trigger is not proven) - the addon, the Lua side and the decoder were fine. Fix: the addon
  paints a new page at least once a second, so a page unchanged for 3 s means a stale capture; the
  reader then moves to PrintWindow (PW_RENDERFULLCONTENT, ~15 ms per 1024x48 grab) and then the
  screen DC, and every 30 s goes back to the window DC if it shows the current page again. The loop
  logs errors and switches (logger `wowharness.pixel`, `events` ring) and restarts itself after an
  exception. `Client.live_health()` / MCP `live_health` say whether new pages arrive (ok, reason,
  new_page_age_s, capture method, switches, restarts, errors, last events); walk_to adds it to its
  "no position" / "lost position" results.
- Follow-up the same day (drv1, 16:14-16:36): with the fix loaded, the reader cycled window ->
  print -> screen and every method kept showing seq 2401, yet `live_health` said ok because a method
  switch reset the clock it used. A full-desktop grab then showed the taskbar clock at 16:13: the whole
  desktop was no longer being composed, while the game's own screenshots showed new sessions and pages.
  The System log has Kernel-Power 566 (system session transition, reason SessionUnlock) at 15:15:28,
  15:54:07 and 16:13:19 - the three freeze times - and InputAccelerometer transitions at 15:29:08
  and 15:57:00, when lines came back. So the laptop's display went off (a TeamViewer "Sponsored
  session" dialog from 16:13 was in front; display-off timeout 5 min on AC, 3 min on battery) and no
  capture method can see the game until the display is on again. Fix: health follows NEW pages only
  (and says when every method shows the same old frame), and the reader thread holds
  ES_DISPLAY_REQUIRED while it runs so the display does not go off; that request does not wake a
  display that is already off (tried once live). `Client.close_live()` stops the reader before a
  module reload replaces the Client.

"""The MCP tool surface. Run on Windows: python -m wowharness.mcp [--config <settings.json>]"""
import functools
import os
import sys
import time

from mcp.server.fastmcp import FastMCP

from .client import Client, ClientNotRunning
from .combat import parse_lines
from .config import DEFAULT_PATH, load_config
from .dashboard import Dashboard, DashboardUnavailable, decision_timeline
from .report import build, summary

app = FastMCP("wowharness")
_state: dict = {}
_config_path: str | None = None


def config_path_from_argv(argv: list[str]) -> str | None:
    """Return the settings path given as `--config <path>` or `--config=<path>`, else None."""
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--config="):
            return arg[len("--config="):]
    return None


def guarded(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        try:
            return {"ok": True, **fn(*a, **k)}
        except (ClientNotRunning, DashboardUnavailable) as err:
            return {"ok": False, "error": str(err)}
    return run


def _client() -> Client:
    if "client" not in _state:
        from .window import Window
        cfg = load_config(_config_path)
        exe = os.path.join(cfg.client_dir, "WoW.exe")
        _state["cfg"] = cfg
        _state["client"] = Client(cfg, lambda: Window.launch(exe, cfg.client_dir))
    return _state["client"]


def _server():
    if "server" not in _state:
        from .server import Server
        c = _state.get("cfg") or load_config(_config_path)
        _state["server"] = Server(c.server_host, c.soap_port, c.soap_user, c.soap_password, c.server_container,
                                  ssh=c.server_ssh)
    return _state["server"]


def _dashboard() -> Dashboard:
    if "dashboard" not in _state:
        cfg = _state.get("cfg") or load_config(_config_path)
        if not cfg.dashboard_url:
            raise DashboardUnavailable(
                f"dashboard not configured (dashboard_url missing in {_config_path or DEFAULT_PATH})")
        _state["dashboard"] = Dashboard(cfg.dashboard_url, cfg.dashboard_user, cfg.dashboard_password)
    return _state["dashboard"]


@app.tool()
@guarded
def launch() -> dict:
    """Write Config.wtf (windowed, no movie, realmlist) and start WoW.exe."""
    return _client().launch()


@app.tool()
@guarded
def login(char_index: int = 0) -> dict:
    """Log the configured human account in and enter the world with the character at char_index."""
    c = _client()
    return c.login(c.cfg.account, c.cfg.password, char_index)


@app.tool()
@guarded
def chat(text: str) -> dict:
    """Type one line into the chat box (Enter, text, Enter)."""
    _client().chat(text)
    return {}


@app.tool()
@guarded
def run_lua(code: str) -> dict:
    """Run /script <code> in the client."""
    _client().run_lua(code)
    return {}


@app.tool()
@guarded
def key(name: str, hold_ms: int = 0) -> dict:
    """Press a key by name (w, a, s, d, space, enter, 1-9, f1-f12) for hold_ms."""
    _client().key(name, hold_ms)
    return {}


@app.tool()
@guarded
def click(fx: float, fy: float) -> dict:
    """Left-click at a fraction of the game window (0..1, from the top left). Moves the real mouse
    cursor there for ~0.3 s: WoW ignores clicks without it. Read positions off a screenshot."""
    _client().click(fx, fy)
    return {}


@app.tool()
@guarded
def screenshot() -> dict:
    """PNG of the game view (the client's own screenshot); returns its Windows path."""
    c = _client()
    c._check()
    path = os.path.join(os.path.expanduser("~"), "wowharness-shots", f"{int(time.time())}.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return {"path": c.screenshot(path)}


@app.tool()
@guarded
def state(live: bool = True) -> dict:
    """One HX| state line from the observer addon. live=True reads it off the pixel strip (no ReloadUI);
    live=False (or no strip within 5 s) uses the SavedVariables path (one ReloadUI, ~5 s)."""
    c = _client()
    if live:
        since = c.live_offset()
        c.chat("/hx")
        line = c.wait_live(r"HX\|p=", since, 5, "chat")
        if line:
            return {"line": line, "via": "pixel"}
        # no strip (old addon, /hx pix off, minimised window): fall back to the ReloadUI path
    since = c.chat_offset()
    c.chat("/hx")
    return {"line": c.wait_chat(r"HX\|p=", since, 15), "via": "reloadui"}


@app.tool()
@guarded
def live_log(since: int = -1, kind: str = "", pattern: str = "", timeout_s: float = 0) -> dict:
    """Chat/combat/HX| lines read live off the addon's pixel strip - no ReloadUI, ~0.1-0.3 s behind the game.
    since=-1 starts the reader (if needed) and returns the current offset only. kind = chat | combat | ""
    (both). With pattern, waits up to timeout_s for a matching line and returns it as `match`.
    `stats` shows pages read, gaps (missed pages = lost lines), corrupt captures and reader CPU."""
    c = _client()
    r = c.live()
    k = kind or None
    if since < 0:
        return {"offset": c.live_offset(), "lines": [], "stats": r.stats()}
    out: dict = {}
    if pattern:
        out["match"] = c.wait_live(pattern, since, max(0.0, min(timeout_s, 300)), k)
    lines, off = c.live_lines(since, k)
    return {**out, "offset": off, "lines": lines, "stats": r.stats()}


@app.tool()
@guarded
def live_health() -> dict:
    """Is the live pixel channel delivering? `health.ok` is false when no new page arrived for 3 s (the
    addon paints one every second): then live reads (state, live_log, walk_to's .gps) see nothing new.
    Shows the capture method (window / print / screen), method switches after stale frames, thread
    restarts, errors and the last events. Check it when live lines seem to stop."""
    return {"health": _client().live_health()}


@app.tool()
@guarded
def tail_combat_log(since: int = -1) -> dict:
    """Combat-log lines since a line offset (-1 = just return the current offset). Each call reloads the UI (~5 s)."""
    c = _client()
    if since < 0:
        return {"offset": c.combat_offset(), "lines": []}
    lines, off, restarted = c.read_combat(since)
    return {"offset": off, "lines": lines, "restarted": restarted}


@app.tool()
@guarded
def tail_chat(since: int = -1) -> dict:
    """Chat lines since a line offset (-1 = just return the current offset). Each call reloads the UI (~5 s)."""
    c = _client()
    if since < 0:
        return {"offset": c.chat_offset(), "lines": []}
    lines, off, restarted = c.read_chat(since)
    return {"offset": off, "lines": lines, "restarted": restarted}


@app.tool()
@guarded
def bot(cmd: str) -> dict:
    """Type a TortoiseBots player command: .bot <cmd>. Returns the TBM:/bot lines seen within 5 s."""
    c = _client()
    since = c.chat_offset()
    c.chat(".bot " + cmd)
    time.sleep(5)
    lines, _, _ = c.read_chat(since)
    return {"lines": lines}


@app.tool()
def gm(cmd: str) -> dict:
    """A GM/console command over SOAP; the reply verbatim."""
    return {"ok": True, "reply": _server().gm(cmd)}


@app.tool()
@guarded
def fight_report(combat_since: int, chat_since: int, me: str, party: list[str]) -> dict:
    """Parse the fight since the given offsets: per-actor totals, bot lines, and a text summary."""
    c = _client()
    lines, _, _ = c.read_combat(combat_since)  # one ReloadUI writes both
    chat_lines, _, _ = c.chat_log.read_since(chat_since)
    rep = build(parse_lines(lines, me), chat_lines, me, party)
    decisions = decision_timeline(_state["watch"], party) if _state.get("watch") else None
    out = {"report": rep, "summary": summary(rep, party, decisions)}
    if decisions is not None:
        out["decisions"] = decisions
    return out


@app.tool()
@guarded
def watch_bots(names: list[str], seconds: int = 30, interval_s: float = 1.0) -> dict:
    """Poll the dashboard for these bots for `seconds` (0-300); blocks for that long. Returns each bot's decision timeline."""
    d = _dashboard()
    seconds = max(0, min(seconds, 300))
    interval_s = max(0.2, interval_s)
    start = time.time()
    samples = []
    while True:
        now = time.time() - start
        samples.append((round(now, 2), d.bots(names)))
        if now + interval_s > seconds:
            break
        time.sleep(interval_s)
    _state["watch"] = samples
    return {"samples": len(samples), "decisions": decision_timeline(samples, names)}


@app.tool()
@guarded
def dashboard(what: str = "bots", names: list[str] | None = None) -> dict:
    """Dashboard data: what = bots | issues | anomalies."""
    d = _dashboard()
    if what == "bots":
        return {"data": d.bots(names)}
    if what == "issues":
        return {"data": d.issues()}
    if what == "anomalies":
        return {"data": d.anomalies()}
    return {"ok": False, "error": f"unknown what: {what} (use bots, issues or anomalies)"}


@app.tool()
@guarded
def scenario_fight(me: str, party: list[str], order: str = ".bot action attack", target: str = "",
                   max_s: int = 60, quiet_s: int = 6) -> dict:
    """ONE call for a whole fight: optional target (living enemy by name), the order, then watch live until the
    party is out of combat for quiet_s (or max_s). Returns report, summary, per-bot decisions and chat - no ReloadUI."""
    from . import scenarios
    return scenarios.fight(_client(), _dashboard(), me, party, order or None, target or None,
                           max(5, min(max_s, 300)), max(2, quiet_s))


@app.tool()
@guarded
def scenario_ride(me: str, party: list[str], go_xyz: str = "", seconds: int = 10) -> dict:
    """ONE call for a movement check: optional GM `.go xyz ...`, then each bot's distance to the player and actions."""
    from . import scenarios
    return scenarios.ride(_client(), _dashboard(), me, party, go_xyz or None, None, max(2, min(seconds, 120)))


def _tiles():
    """The server's navmesh tiles, copied on demand (read-only ssh) into a local cache."""
    if "tiles" not in _state:
        from .navwalk import tiles_for
        _state["tiles"] = tiles_for(_state.get("cfg") or load_config(_config_path))
    return _state["tiles"]


@app.tool()
@guarded
def walk_to(x: float, y: float, z: float, map_id: int = -1, timeout_s: float = 0, arrive: float = 2.0,
            engine: str = "auto", avoid: list[list[float]] | None = None) -> dict:
    """Walk the player to (x, y, z) with the keyboard along the server's navmesh (the bots' mmaps): .gps fixes
    (GM; target put aside and restored), turns with A/D, W to walk, unstick and re-plan when stuck. Works in
    dungeons. map_id -1 = the map the player is on. timeout_s 0 = from the path length. Returns ok/reason,
    path, trace, stuck events. A closed door (the navmesh has no game objects) ends it with reason "blocked
    (door or object?)" and result["blocked"]; pass its "ahead" point in avoid=[[x, y, z]] to route round it.
    Check a route first with nav_path."""
    from . import navwalk
    try:
        return navwalk.walk_to(_client(), None, x, y, z, None if map_id < 0 else map_id, tiles=_tiles(),
                               arrive=max(0.5, arrive), timeout_s=timeout_s or None, engine=engine, avoid=avoid)
    except (OSError, ValueError) as err:
        return {"ok": False, "error": f"{type(err).__name__}: {err}"}


@app.tool()
@guarded
def nav_path(x: float, y: float, z: float, map_id: int = -1, from_x: float = 0, from_y: float = 0,
             from_z: float = 0, engine: str = "auto", avoid: list[list[float]] | None = None) -> dict:
    """The navmesh path walk_to would take, without walking. From the player's .gps position, or from
    (from_x, from_y, from_z) when map_id is given and from_* are not all 0."""
    from . import navwalk
    try:
        if map_id >= 0 and (from_x or from_y or from_z):
            start, m = (from_x, from_y, from_z), map_id
        else:
            src = navwalk.GpsPose(_client())
            fix = src.read()
            if fix is None:
                return {"ok": False, "error": "no .gps reply"}
            start, m = fix.xyz, (fix.map_id if map_id < 0 else map_id)
        return {"from": start, "map": m, **navwalk.plan_path(_tiles(), m, start, (x, y, z), engine=engine, avoid=avoid)}
    except (OSError, ValueError) as err:
        return {"ok": False, "error": f"{type(err).__name__}: {err}"}


@app.tool()
@guarded
def scenario_walk(party: list[str], x: float, y: float, z: float, map_id: int = -1, timeout_s: float = 0) -> dict:
    """ONE call for a follow test: walk_to (x, y, z) and sample the party bots on the dashboard meanwhile.
    Returns the walk result plus each bot's distance to the player over time and its worst distance."""
    from . import scenarios
    try:
        return scenarios.walk(_client(), _dashboard(), party, x, y, z, None if map_id < 0 else map_id,
                              tiles=_tiles(), timeout_s=timeout_s or None)
    except (OSError, ValueError) as err:
        return {"ok": False, "error": f"{type(err).__name__}: {err}"}


@app.tool()
@guarded
def ask_bots(party: list[str], text: str = "nc ?", wait_s: int = 4) -> dict:
    """Whisper every bot the same text (e.g. "nc ?", "co ?") and return each bot's replies (live)."""
    from . import scenarios
    return {"replies": scenarios.ask_bots(_client(), party, text, max(1, min(wait_s, 30)))}


@app.tool()
@guarded
def hire_party(specs: list[str], wait_s: int = 40) -> dict:
    """`.bot hire <spec>` for each spec and wait until they joined (live). Example specs: ["warrior tank", "priest holy"]."""
    from . import scenarios
    return scenarios.hire_party(_client(), None, specs, max(5, min(wait_s, 120)))


if __name__ == "__main__":
    _config_path = config_path_from_argv(sys.argv[1:])
    app.run()

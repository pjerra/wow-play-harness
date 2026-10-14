"""MCP tools with the harness's names, backed by turtle-agent (no game window).

Run: python -m wowharness.mcp_headless [--rpc 127.0.0.1:8765] [--spell-names names.json]
                                        [--dashboard-config ~/.wowharness.json]
turtle-agent must already be logged in (turtle-agent login <account> <character>). The bot dashboard (only
fight_timeline uses it) logs in from BUILDGATE_DASHBOARD_URL / _USER / _PASSWORD, else the dashboard_* keys
of --dashboard-config, as tools/buildgate.py does."""
import functools
import importlib.util
import os
import sys
import time

from mcp.server.fastmcp import FastMCP

from . import timeline
from .dashboard import Dashboard, DashboardUnavailable
from .headless import DEFAULT_RPC, HeadlessClient, HeadlessError, load_spell_names

app = FastMCP("wowharness-headless")
_state: dict = {"rpc": DEFAULT_RPC, "spell_names": {}, "dashboard_config": None}


def option_from_argv(argv: list[str], name: str) -> str | None:
    """The value of `name VALUE` or `name=VALUE`, else None."""
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
    return None


def guarded(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        try:
            return {"ok": True, **fn(*a, **k)}
        except (HeadlessError, DashboardUnavailable) as err:
            return {"ok": False, "error": str(err)}
    return run


def _c() -> HeadlessClient:
    if "client" not in _state:
        _state["client"] = HeadlessClient(_state["rpc"], spell_names=_state["spell_names"])
    return _state["client"]


def _dashboard() -> Dashboard:
    """The bot dashboard, logged in the way tools/buildgate.py does it (environment, else the settings file)."""
    if "dashboard" not in _state:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "buildgate.py")
        spec = importlib.util.spec_from_file_location("_buildgate_cli", path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        url, user, password = cli.dashboard_settings(None, _state["dashboard_config"])
        if not url:
            raise DashboardUnavailable("no dashboard URL (BUILDGATE_DASHBOARD_URL or dashboard_url in the settings file)")
        _state["dashboard"] = Dashboard(url, user, password)
    return _state["dashboard"]


@app.tool()
@guarded
def chat(text: str) -> dict:
    """A chat-box line: '.' commands, /p /s /y /w Name text, or plain say. No Lua in headless mode."""
    _c().chat(text)
    return {}


@app.tool()
@guarded
def gm(cmd: str) -> dict:
    """A GM command run by the character in game; returns the system reply lines."""
    return _c().gm(cmd)


@app.tool()
@guarded
def bot(cmd: str) -> dict:
    """A TortoiseBots player command: .bot <cmd>. Returns the chat lines seen within 5 s."""
    return _c().bot(cmd)


@app.tool()
@guarded
def state(live: bool = True) -> dict:
    """One HX| state line (player, target, party). `live` is accepted for parity and ignored."""
    return _c().state()


@app.tool()
@guarded
def tail_combat_log(since: int = -1) -> dict:
    """Combat-log lines since an event offset (-1 = just return the current offset)."""
    return _c().tail_combat_log(since)


@app.tool()
@guarded
def tail_chat(since: int = -1) -> dict:
    """Chat lines since an event offset (-1 = just return the current offset)."""
    return _c().tail_chat(since)


@app.tool()
@guarded
def fight_report(combat_since: int, chat_since: int, me: str, party: list[str]) -> dict:
    """Per-actor totals, bot lines and a text summary for the fight since the offsets."""
    return _c().fight_report(combat_since, chat_since, me, party)


@app.tool()
@guarded
def status() -> dict:
    """turtle-agent's session status (state, position, health, latency, parse errors)."""
    return {"status": _c().call("status")}


@app.tool()
@guarded
def units(near: float = 40, hostile: bool | None = None, name: str = "", limit: int = 50) -> dict:
    """Units around the player, nearest first."""
    params = {"near": near, "limit": limit}
    if hostile is not None:
        params["hostile"] = hostile
    if name:
        params["name"] = name
    return {"units": _c().call("units", **params)}


@app.tool()
@guarded
def party() -> dict:
    """Party members with health, power, level and online state."""
    return {"party": _c().call("party")}


@app.tool()
@guarded
def invite(name: str) -> dict:
    """Invite a character to the party."""
    return {"result": _c().call("invite", name=name)}


@app.tool()
@guarded
def accept_invite() -> dict:
    """Accept the pending party invite."""
    return {"result": _c().call("accept_invite")}


@app.tool()
@guarded
def leave_group() -> dict:
    """Leave the party."""
    return {"result": _c().call("leave_group")}


@app.tool()
@guarded
def target(name: str = "", guid: str = "") -> dict:
    """Select a unit by name (nearest match) or guid."""
    params = {"guid": guid} if guid else {"name": name}
    return {"unit": _c().call("target", **params)}


@app.tool()
@guarded
def attack() -> dict:
    """Start auto-attack on the current target."""
    _c().call("attack")
    return {}


@app.tool()
@guarded
def stop_attack() -> dict:
    """Stop auto-attack."""
    _c().call("stop_attack")
    return {}


@app.tool()
@guarded
def cast(spell_id: int, target: str = "") -> dict:
    """Cast a spell by id on a unit guid, else on the current target, else on the player."""
    params = {"spell_id": spell_id}
    if target:
        params["target"] = target
    _c().call("cast", **params)
    return {}


@app.tool()
@guarded
def face(guid: str) -> dict:
    """Turn towards a unit."""
    _c().call("face", guid=guid)
    return {}


@app.tool()
@guarded
def walk_to(x: float, y: float, z: float) -> dict:
    """Walk in a straight line to a point at most 40 yards away."""
    _c().call("walk_to", x=x, y=y, z=z)
    return {}


@app.tool()
@guarded
def teleport(x: float, y: float, z: float, map: int = -1) -> dict:
    """GM teleport (.go xyz); map -1 keeps the current map."""
    params = {"x": x, "y": y, "z": z}
    if map >= 0:
        params["map"] = map
    _c().call("teleport", **params)
    return {}


@app.tool()
@guarded
def logout() -> dict:
    """Log the character out; turtle-agent exits afterwards."""
    _c().call("logout")
    return {}


@app.tool()
@guarded
def fight_timeline(seconds: int = 60, names: list[str] | None = None, order: str = "", interval_s: float = 1.0,
                   label: str = "", scenario: str = "fight", out_dir: str = "") -> dict:
    """Record a fight window and return it as a per-second timeline (markdown and JSON). Blocks `seconds`
    (1-300): polls the bot dashboard, the player and units within 100 yd every interval_s, sends `order` (a GM
    or .bot command, e.g. '.bot attack Latal'; marks the pull) after the first sample, then merges turtle-agent's
    combat and chat events of the window. Per bot and second: action (trigger), moved, distance to its
    target, HP and mana %, records it caused and took. Annotations: reach-action standstills of 5 s+ out of
    range, Polymorph starts vs completions, first-heal latency after a member drops below 70 %, deaths with
    their last 5 s. names: the party bots (default: the whole party). out_dir: also write
    timeline-<label>.md/.json there (read them back with tools/compare_runs.py)."""
    label = label or time.strftime("%Y%m%d-%H%M%S")
    t = timeline.record_fight(_c(), _dashboard(), names or None, max(1, min(int(seconds), 300)),
                              max(0.2, float(interval_s)), order, label, scenario,
                              wall=_state.get("wall", time.time), sleep=_state.get("sleep", time.sleep))
    out = {"markdown": timeline.render_markdown(t), "metrics": t["metrics"], "timeline": t}
    if out_dir:
        out["files"] = list(timeline.write(t, out_dir))
    return out


if __name__ == "__main__":
    _state["rpc"] = option_from_argv(sys.argv[1:], "--rpc") or DEFAULT_RPC
    _state["dashboard_config"] = option_from_argv(sys.argv[1:], "--dashboard-config")
    names_file = option_from_argv(sys.argv[1:], "--spell-names")
    if names_file:
        _state["spell_names"] = load_spell_names(names_file)
    app.run()

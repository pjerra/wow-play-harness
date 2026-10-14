"""One-call test scenarios, so a tester does a whole step (fight, ride, party check) in one tool call
instead of 10-20 small calls with thinking in between. All reads go over the live pixel channel and the
dashboard: no ReloadUI.

Every function takes the objects the MCP layer already holds (client, dashboard) so it can be used from
the MCP tools below and from a driver session: `from wowharness import scenarios as sc`.
"""
import threading
import time

from .combat import parse_lines
from .dashboard import decision_timeline
from .headless import HeadlessClient
from .report import build, summary


class _Sampler:
    """Poll the dashboard for some bots every `interval_s` in a background thread."""

    def __init__(self, dashboard, names, interval_s=1.0):
        self.d, self.names, self.interval = dashboard, names, max(0.3, interval_s)
        self.samples, self._stop, self._t0 = [], threading.Event(), time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.samples.append((round(time.time() - self._t0, 2), self.d.bots(self.names)))
            except Exception:  # a dashboard hiccup must not end the scenario
                pass
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


def _party_in_combat(samples):
    last = samples[-1][1] if samples else []
    return any((b.get("state") or "") == "combat" for b in last)


def fight(client, dashboard, me, party, order=".bot action attack", target=None,
          max_s=60, quiet_s=6, interval_s=1.0):
    """Target (optionally a living enemy by name), give the order, then watch until the party has been out of
    combat for `quiet_s` or `max_s` passed. Returns the parsed fight report, a text summary, each bot's
    decision timeline and the raw chat lines - all from live data."""
    since = client.live_offset()
    if target and isinstance(client, HeadlessClient):  # the nearest living one, like HX_TargetLive
        alive = [u for u in client.call("units", name=target) if not u.get("dead")]
        client.call("target", **({"guid": alive[0]["guid"]} if alive else {"name": target}))
    elif target:
        client.run_lua(f'HX_TargetLive("{target}")')
        time.sleep(0.5)
    if order:
        client.chat(order)
    t0, calm_since = time.time(), None
    with _Sampler(dashboard, party, interval_s) as s:
        time.sleep(min(3.0, max_s))  # give the order time to start the fight
        while time.time() - t0 < max_s:
            if _party_in_combat(s.samples):
                calm_since = None
            elif calm_since is None:
                calm_since = time.time()
            elif time.time() - calm_since >= quiet_s:
                break
            time.sleep(interval_s)
    combat, _ = client.live_lines(since, "combat")
    chat, _ = client.live_lines(since, "chat")
    rep = build(parse_lines(combat, me), chat, me, party)
    decisions = decision_timeline(s.samples, party)
    return {"seconds": round(time.time() - t0, 1), "report": rep,
            "summary": summary(rep, party, decisions), "decisions": decisions,
            "chat": chat[-60:], "combat_lines": len(combat)}


def ride(client, dashboard, me, party, go_xyz=None, keys=None, seconds=10, interval_s=1.0):
    """Mount-free movement check: move the player (a GM `.go xyz x y z [map]` string or a list of
    (key, hold_ms) presses), then report each bot's distance to the player and actions over `seconds`."""
    since = client.live_offset()
    with _Sampler(dashboard, party + [me], interval_s) as s:
        if go_xyz:
            client.chat(go_xyz)
        for k, ms in (keys or []):
            client.key(k, ms)
        time.sleep(seconds)
    rows = []
    for t, bots in s.samples:
        me_row = next((b for b in bots if b.get("name") == me), None)
        for b in bots:
            if b.get("name") == me or not me_row:
                continue
            dx, dy = (b.get("x") or 0) - (me_row.get("x") or 0), (b.get("y") or 0) - (me_row.get("y") or 0)
            rows.append({"t": t, "bot": b.get("name"), "dist": round((dx * dx + dy * dy) ** 0.5, 1),
                         "state": b.get("state"), "action": b.get("last_action")})
    chat, _ = client.live_lines(since, "chat")
    return {"distances": rows, "decisions": decision_timeline(s.samples, party), "chat": chat[-40:]}


def ask_bots(client, party, text="nc ?", wait_s=4):
    """Whisper every party bot the same text (e.g. "nc ?", "co ?", "talents") and return their replies."""
    since = client.live_offset()
    for name in party:
        client.chat(f"/w {name} {text}")
        time.sleep(0.3)
    time.sleep(wait_s)
    chat, _ = client.live_lines(since, "chat")
    return {name: [l for l in chat if f"[{name}] whispers:" in l] for name in party}


def hire_party(client, dashboard, specs, wait_s=40):
    """`.bot hire <spec>` for each spec (e.g. ["warrior tank", "priest holy"]) and wait until that many
    companions are in the party (from the live HX state line). Returns the joined names and the chat."""
    since = client.live_offset()
    for spec in specs:
        client.chat(f".bot hire {spec}")
        time.sleep(1.5)
    deadline, joined = time.time() + wait_s, []
    while time.time() < deadline:
        chat, _ = client.live_lines(since, "chat")
        joined = [l.split("  ", 1)[-1].split(" joins the party")[0] for l in chat if " joins the party" in l]
        if len(joined) >= len(specs):
            break
        time.sleep(1)
    chat, _ = client.live_lines(since, "chat")
    return {"joined": joined, "chat": chat[-40:]}


def walk(client, dashboard, party, x, y, z, map_id=None, tiles=None, interval_s=1.0, **walk_kw):
    """Walk the player to (x, y, z) along the navmesh (navwalk.walk_to) while sampling the party bots on the
    dashboard: how far each bot was from the player through the walk (follow test without GM teleports).
    Distances pair each dashboard sample with the walk's nearest position fix in time."""
    from .navwalk import walk_to
    with _Sampler(dashboard, party, interval_s) as s:
        t_start = time.time()
        res = walk_to(client, None, x, y, z, map_id, tiles=tiles, **walk_kw)
        time.sleep(min(3.0, interval_s * 2))  # let the bots catch up for the last samples
    t_off = t_start - s._t0  # walk trace times are from t_start, sampler times from s._t0
    trace = res.get("trace") or []
    rows, worst = [], {}
    for t, bots in s.samples:
        if not trace:
            break
        me = min(trace, key=lambda p: abs(p["t"] + t_off - t))
        for b in bots:
            if b.get("x") is None:
                continue
            d = round(((b["x"] - me["x"]) ** 2 + (b["y"] - me["y"]) ** 2) ** 0.5, 1)
            rows.append({"t": round(t - t_off, 1), "bot": b.get("name"), "dist": d, "state": b.get("state"),
                         "action": b.get("last_action")})
            worst[b.get("name")] = max(worst.get(b.get("name"), 0), d)
    return {"walk": res, "follow": rows, "max_dist": worst, "decisions": decision_timeline(s.samples, party)}

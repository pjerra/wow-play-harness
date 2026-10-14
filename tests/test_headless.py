import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wowharness.combat import parse_lines
from wowharness.headless import HeadlessClient, HeadlessError

T = "2026-10-12T21:03:40.120000000+00:00"
NAMES = {2050: "Lesser Heal", 133: "Fireball", 1130: "Hunter's Mark"}


def ev(seq, kind, **kw):
    e = {"seq": seq, "time": T, "kind": kind}
    e.update(kw)
    return e


# The same fight as tests/test_report.py, as turtle-agent events.
EVENTS = [
    ev(1, "melee", source="Gorm", target="Kobold Vermin", amount=12, data={"outcome": "normal"}),
    ev(2, "melee", source="Kobold Vermin", target="Gorm", amount=4, data={"outcome": "normal"}),
    ev(3, "spell_start", source="Lia", spell_id=2050),
    ev(4, "heal", source="Lia", target="Gorm", spell_id=2050, amount=48),
    ev(5, "spell_go", source="Lia", target="Gorm", spell_id=2050, data={"hits": 1}),
    ev(6, "spell_damage", source="Harnessb", target="Kobold Vermin", spell_id=133, amount=33, data={"school": 2}),
    ev(7, "aura_applied", target="Kobold Vermin", spell_id=1130, data={"harmful": True, "slot": 32}),
    ev(8, "death", source="Kobold Vermin"),
    ev(9, "whisper", source="Gorm", text="Attacking Kobold Vermin", data={"type": "whisper"}),
    ev(10, "system", text="TBM:ACTION_ACK|attack|bot:Gorm|1", data={"type": "system"}),
]


class RpcError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


class Agent:
    """A scripted turtle-agent: answers the JSON-RPC methods the wrapper uses."""

    def __init__(self):
        self.calls, self.events, self.times = [], list(EVENTS), []

    def add(self, kind, **kw):
        self.events.append(ev(len(self.events) + 1, kind, **kw))

    def handle(self, method, params):
        self.calls.append((method, params))
        self.times.append(time.monotonic())
        if method == "status":
            return {"state": "in_world", "character": "Harnessb", "last_seq": len(self.events), "map": 0,
                    "x": 1.0, "y": 2.0, "z": 0.0, "health": 50, "max_health": 100, "power": 30, "max_power": 60}
        if method == "events":
            kinds = params.get("kinds") or []
            evs = [e for e in self.events if e["seq"] > params.get("since", 0) and (not kinds or e["kind"] in kinds)]
            return {"events": evs, "next": len(self.events), "oldest": 1, "dropped": False, "more": False}
        if method == "party":
            return {"in_group": True, "members": [{"name": "Gorm", "health": 90, "max_health": 100},
                                                  {"name": "Lia", "health": 100, "max_health": 100}]}
        if method == "units":
            return [{"guid": "0xF130000000000001", "name": "Kobold Vermin", "health": 0, "max_health": 50, "dead": True}]
        if method == "target":
            return {"guid": "0xF130000000000001", "name": "Kobold Vermin", "health": 50, "max_health": 50,
                    "x": 11.0, "y": 2.0, "z": 0.0}
        if method == "gm":
            if params["text"] == ".fail":
                raise RpcError(-32003, "boom")
            if params["text"].startswith(".bot version"):
                self.add("system", text="TBM:VERSION|2026-10-10-v3", data={"type": "system"})
            elif params["text"].startswith(".gm"):
                self.add("system", text="You are now visible.", data={"type": "system"})
            return {}
        if method == "attack":  # the fight happens: replay the scripted combat and chat events
            for e in EVENTS:
                self.add(e["kind"], **{k: v for k, v in e.items() if k not in ("seq", "time", "kind")})
            return {}
        if method in ("chat", "stop_attack", "invite", "teleport", "walk_to", "cast", "face"):
            return {}
        raise RpcError(-32601, "Method not found: " + method)


@pytest.fixture
def agent():
    a = Agent()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                out = {"jsonrpc": "2.0", "id": req["id"], "result": a.handle(req["method"], req.get("params") or {})}
            except RpcError as e:
                out = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": e.code, "message": e.msg}}
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    a.rpc = f"127.0.0.1:{srv.server_address[1]}"
    yield a
    srv.shutdown()


def test_chat_routes_like_the_chat_box(agent):
    c = HeadlessClient(agent.rpc)
    c.chat("/p hi")
    c.chat(".bot version")
    c.chat("/w Gorm come here")
    c.chat("hello")
    sent = [x for x in agent.calls if x[0] in ("chat", "gm")]
    assert sent == [("chat", {"type": "party", "text": "hi"}), ("gm", {"text": ".bot version"}),
                    ("chat", {"type": "whisper", "to": "Gorm", "text": "come here"}), ("chat", {"type": "say", "text": "hello"})]
    with pytest.raises(HeadlessError):
        c.chat("/script print(1)")
    with pytest.raises(ValueError):
        c.chat("x" * 256)


def test_errors(agent):
    with pytest.raises(HeadlessError) as err:
        HeadlessClient(agent.rpc).chat(".fail")
    assert err.value.code == -32003
    with pytest.raises(HeadlessError, match="not reachable"):
        HeadlessClient("127.0.0.1:1", timeout_s=1).call("status")


def test_tail_combat_log_lines_parse_back(agent):
    c = HeadlessClient(agent.rpc, spell_names=NAMES)
    assert c.tail_combat_log() == {"offset": 10, "lines": []}
    out = c.tail_combat_log(0)
    kinds = [e.kind for e in parse_lines(out["lines"], "Harnessb")]
    assert kinds == ["melee", "melee", "cast_start", "heal", "cast", "spell", "aura", "dies"]
    assert out["lines"][0].endswith("  Gorm hits Kobold Vermin for 12.") and out["restarted"] is False


def test_tail_chat_lines(agent):
    lines = HeadlessClient(agent.rpc).tail_chat(0)["lines"]
    assert lines[0].endswith("  [Gorm] whispers: Attacking Kobold Vermin")
    assert lines[1].endswith("  TBM:ACTION_ACK|attack|bot:Gorm|1")


def test_fight_report_matches_the_real_report(agent):
    out = HeadlessClient(agent.rpc, spell_names=NAMES).fight_report(0, 0, "Harnessb", ["Gorm", "Lia"])
    a = out["report"]["actors"]
    assert a["Gorm"]["damage_done"] == 12 and a["Gorm"]["damage_taken"] == 4
    assert a["Lia"]["healing_done"] == 48 and a["Lia"]["casts"] == {"Lesser Heal": 1}
    assert a["Harnessb"]["damage_done"] == 33 and a["Harnessb"]["casts"] == {"Fireball": 1}
    assert out["report"]["killed"] == ["Kobold Vermin"] and out["report"]["auras"] == ["Hunter's Mark on Kobold Vermin"]
    assert len(out["report"]["bot_lines"]) == 2
    assert "Gorm: 12 damage" in out["summary"] and "Lia: healed 48" in out["summary"]


def test_state_gm_and_bot(agent):
    c = HeadlessClient(agent.rpc)
    st = c.state()
    assert st["line"] == "HX|p=Harnessb,50,50,1.0,2.0,map0|party=Gorm:90,Lia:100" and st["via"] == "headless"
    assert c.gm(".gm visible on", wait_s=0.3) == {"ok": True, "reply": "You are now visible."}
    assert any("TBM:VERSION|" in l for l in c.bot("version", wait_s=0.3)["lines"])


def test_chat_needs_text(agent):
    c = HeadlessClient(agent.rpc)
    for bad in ("/w Gorm", "/w", "/p", "/p  "):
        with pytest.raises(ValueError):
            c.chat(bad)
    assert not [x for x in agent.calls if x[0] == "chat"]


def test_gm_reports_ok(agent):
    assert HeadlessClient(agent.rpc).gm(".gm visible on", wait_s=0.3)["ok"] is True


def test_live_offset_lines_and_wait(agent):
    c = HeadlessClient(agent.rpc, spell_names=NAMES)
    assert c.live_offset() == 10
    chat, nxt = c.live_lines(0, "chat")
    assert nxt == 10 and len(chat) == 2 and "whispers" in chat[0]
    combat, _ = c.live_lines(0, "combat")
    assert len(combat) == 8 and combat[0].endswith("Gorm hits Kobold Vermin for 12.")
    both, _ = c.live_lines(0)
    assert len(both) == 10
    assert c.live_lines(10, "chat") == ([], 10)
    assert "Attacking" in c.wait_live(r"whispers: Attacking", 0, 1, "chat")
    assert c.wait_live(r"Fireball", 0, 1, "chat") is None
    assert "Fireball" in c.wait_live(r"Fireball", 0, 1)


def test_scenarios_ask_bots_over_headless(agent, monkeypatch):
    from wowharness import scenarios
    monkeypatch.setattr(scenarios.time, "sleep", lambda s: None)
    orig = agent.handle

    def handle(method, params):
        out = orig(method, params)
        if method == "chat" and params.get("type") == "whisper":
            agent.add("whisper", source=params["to"], text="Strategies: follow", data={"type": "whisper"})
        return out

    agent.handle = handle
    r = scenarios.ask_bots(HeadlessClient(agent.rpc), ["Gorm", "Lia"], "nc ?", 0)
    assert len(r["Gorm"]) == 1 and "Strategies: follow" in r["Gorm"][0] and len(r["Lia"]) == 1


def test_spell_miss_fail_and_power_ticks_render():
    from wowharness.combat import parse_line
    from wowharness.headless import COMBAT_KINDS, combat_line, to_report_events
    assert "spell_miss" in COMBAT_KINDS and "spell_fail" in COMBAT_KINDS
    names = {**NAMES, 5138: "Drain Mana", 2687: "Bloodrage", 5217: "Tiger's Fury"}
    miss = dict(source="Harnessb", target="Kobold Vermin", spell_id=133)
    cases = [
        (ev(1, "spell_miss", **miss, data={"miss": "miss"}), "Harnessb's Fireball misses Kobold Vermin."),
        (ev(2, "spell_miss", **miss, data={"miss": "resist"}), "Harnessb's Fireball was resisted by Kobold Vermin."),
        (ev(3, "spell_miss", **miss, data={"miss": "dodge"}), "Harnessb's Fireball was dodged by Kobold Vermin."),
        (ev(4, "spell_miss", **miss, data={"miss": "parry"}), "Harnessb's Fireball was parried by Kobold Vermin."),
        (ev(5, "spell_miss", **miss, data={"miss": "block"}), "Harnessb's Fireball was blocked by Kobold Vermin."),
        (ev(6, "spell_miss", **miss, data={"miss": "evade"}), "Harnessb's Fireball was evaded by Kobold Vermin."),
        (ev(7, "spell_miss", **miss, data={"miss": "immune"}), "Harnessb's Fireball fails. Kobold Vermin is immune."),
        (ev(8, "spell_miss", **miss, data={"miss": "deflect"}), "Harnessb's Fireball was deflected by Kobold Vermin."),
        (ev(9, "spell_miss", **miss, data={"miss": "absorb"}), "Harnessb's Fireball is absorbed by Kobold Vermin."),
        (ev(10, "spell_miss", **miss, data={"miss": "reflect"}), "Harnessb's Fireball is reflected back by Kobold Vermin."),
        (ev(11, "spell_miss", **miss, data={"miss": "miss_13"}), "Harnessb's Fireball misses Kobold Vermin."),
        (ev(12, "spell_miss", **miss, data={"miss": "resist", "proc": True}), "Kobold Vermin resists Harnessb's Fireball."),
        (ev(13, "spell_fail", source="Harnessb", spell_id=133, data={"reason": 12}), "Harnessb fails to cast Fireball: error 12."),
        (ev(14, "spell_fail", source="Lia", spell_id=2050, data={"reason": "interrupted"}),
         "Lia fails to cast Lesser Heal: Interrupted."),
        (ev(15, "energize", source="Gorm", target="Gorm", spell_id=2687, amount=10, data={"power": 1}),
         "Gorm gains 10 Rage from Gorm's Bloodrage."),
        (ev(16, "periodic", source="Lia", target="Gorm", spell_id=5217, amount=20, data={"tick": "energize", "power": 3}),
         "Gorm gains 20 Energy from Lia's Tiger's Fury."),
        (ev(17, "periodic", source="Harnessb", target="Kobold Vermin", spell_id=5138, amount=30,
            data={"tick": "leech", "power": 0}), "Harnessb's Drain Mana drains 30 Mana from Kobold Vermin."),
    ]
    spell = lambda i: names.get(i, f"spell {i}")
    for e, want in cases:
        line = combat_line(e, spell)
        assert line.split("  ", 1)[1] == want
    # Misses and failures reach the report the way the real client's combat log does: as lines
    # combat.parse_line keeps (kind 'other', counted under unparsed), never as damage or casts.
    for e, _ in cases[:14]:
        got = to_report_events([e], spell)
        assert [x.kind for x in got] == [parse_line(combat_line(e, spell), "Harnessb").kind] == ["other"]
        assert got[0].raw == combat_line(e, spell)


def test_misses_reach_tail_and_fight_report(agent):
    agent.add("spell_miss", source="Harnessb", target="Kobold Vermin", spell_id=133, data={"miss": "resist"})
    c = HeadlessClient(agent.rpc, spell_names=NAMES)
    assert c.tail_combat_log(10)["lines"][0].endswith("Harnessb's Fireball was resisted by Kobold Vermin.")
    rep = c.fight_report(0, 0, "Harnessb", ["Gorm", "Lia"])["report"]
    assert rep["unknown"] == 1 and rep["actors"]["Harnessb"]["damage_done"] == 33


def test_key_and_run_lua_need_the_real_client(agent):
    c = HeadlessClient(agent.rpc)
    with pytest.raises(HeadlessError, match="needs the real client"):
        c.key("w", 100)
    with pytest.raises(HeadlessError, match="needs the real client"):
        c.run_lua('HX_TargetLive("Kobold Vermin")')
    assert agent.calls == []


def test_live_lines_both_kinds_lose_nothing_between_reads(agent):
    orig = agent.handle

    def handle(method, params):  # a combat event lands right after every events read
        out = orig(method, params)
        if method == "events":
            agent.add("melee", source="Gorm", target="Kobold Vermin", amount=7, data={"outcome": "normal"})
        return out

    agent.handle = handle
    c = HeadlessClient(agent.rpc, spell_names=NAMES)
    lines, nxt = c.live_lines(0)
    later, nxt = c.live_lines(nxt)
    hits = [l for l in lines + later if l.endswith("Gorm hits Kobold Vermin for 7.")]
    assert hits and len(hits) == len([e for e in agent.events if e.get("amount") == 7 and e["seq"] <= nxt])


def test_wait_live_unknown_kind_and_zero_timeout(agent):
    c = HeadlessClient(agent.rpc, spell_names=NAMES)
    assert c.wait_live(r".", 0, 5, "party") is None
    assert not [x for x in agent.calls if x[0] == "events"]
    assert "Fireball" in c.wait_live(r"Fireball", 0, 0)


def test_scenarios_fight_targets_over_rpc(agent, monkeypatch):
    from wowharness import scenarios
    monkeypatch.setattr(scenarios.time, "sleep", lambda s: None)
    r = scenarios.fight(HeadlessClient(agent.rpc, spell_names=NAMES), None, "Harnessb", ["Gorm", "Lia"],
                        order=None, target="Kobold Vermin", max_s=0.05, quiet_s=0)
    assert ("target", {"name": "Kobold Vermin"}) in agent.calls  # only a dead one around: by name
    assert not [x for x in agent.calls if x[0] in ("chat", "gm")]
    assert r["combat_lines"] == 0
    orig = agent.handle
    agent.handle = lambda m, p: ([{"guid": "0xF130000000000002", "name": "Kobold Vermin", "dead": False}]
                                 if m == "units" else orig(m, p))
    scenarios.fight(HeadlessClient(agent.rpc), None, "Harnessb", [], order=None, target="Kobold Vermin", max_s=0.05)
    assert ("target", {"guid": "0xF130000000000002"}) in agent.calls

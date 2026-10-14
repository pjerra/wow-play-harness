"""Fight timeline: the merge of dashboard samples and combat/chat events into per-second rows, and the
annotations (out-of-range standstill, CC starts vs completions, first-heal latency, deaths), on tonight's
recordings (tests/fixtures/buildgate) and on fake streams. No live runs."""
import json
import os
from datetime import datetime, timezone

from wowharness import buildgate as bg
from wowharness import timeline as tl

HERE = os.path.dirname(__file__)
FIX = os.path.join(HERE, "fixtures", "buildgate")
T0 = 1_791_810_000.0
POLY = 12826


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def bot(name, x, y, *, action="check values", trigger="", state="combat", target="", hp=100, max_hp=100,
        power=None, max_power=None, power_type="mana", map_id=70):
    b = {"name": name, "map": map_id, "x": x, "y": y, "state": state, "last_action": action, "last_trigger": trigger,
         "target": target, "hp": hp, "max_hp": max_hp, "power_type": power_type}
    if power is not None:
        b.update(power=power, max_power=max_power)
    return b


def sample(t, bots, leader=None, units=()):
    return {"t": t, "leader": leader, "bots": {b["name"].lower(): b for b in bots}, "units": list(units)}


def ev(dt, kind, **kw):
    return {"seq": 0, "time": datetime.fromtimestamp(T0 + dt, timezone.utc).isoformat(), "kind": kind, **kw}


# ---------------------------------------------------------------- the merge

def test_rows_merge_samples_and_events_per_second():
    oracle = {"name": "Stonevault Oracle", "guid": "0xF1", "x": 30.0, "y": 0.0, "dead": False}
    samples = [sample(0.2, [bot("Mae", 0, 0, action="reach spell", trigger="enemy out of spell range",
                                target="Stonevault Oracle", power=800, max_power=1000),
                            bot("Lia", 5, 0, action="renew", target="Mae", hp=50)], units=[oracle]),
               sample(1.1, [bot("Mae", 4, 0, action="frostbolt", trigger="high aoe", target="Stonevault Oracle",
                                power=700, max_power=1000),
                            bot("Lia", 5, 0, action="renew", target="Mae", hp=50)], units=[oracle])]
    evs = [ev(1.3, "spell_damage", source="Mae", target="Stonevault Oracle", spell_id=116, amount=225, party=True),
           ev(1.6, "melee", source="Stonevault Oracle", target="Lia", amount=40, data={"outcome": "normal"}),
           ev(1.7, "heal", source="Lia", target="Lia", spell_id=139, amount=60, party=True),
           ev(1.8, "whisper", source="Mae", text="Out of range")]
    recs = tl.align(tl.records_from_agent_events(evs, ["Mae", "Lia"]), T0)
    out = tl.build_timeline(samples, recs, ["Mae", "Lia"])
    assert [r["t"] for r in out["rows"]] == [0, 1]
    m0, m1 = out["rows"][0]["bots"]["Mae"], out["rows"][1]["bots"]["Mae"]
    assert m0["action"] == "reach spell" and m0["trigger"] == "enemy out of spell range"
    assert m0["dist"] == 30.0 and m0["mana"] == 80 and m0["hp"] == 100 and m0["moved"] is None
    assert m1["moved"] is True and m1["dist"] == 26.0 and m1["mana"] == 70
    assert m1["caused"] == ["damage Stonevault Oracle 225"] and m1["taken"] == []
    lia = out["rows"][1]["bots"]["Lia"]
    assert lia["moved"] is False and lia["dist"] == 1.0  # the target is a party member: its own position
    assert lia["taken"] == ["damage from Stonevault Oracle 40", "heal from Lia 60"]
    assert lia["caused"] == ["heal Lia 60"]
    assert out["rows"][1]["chat"] == ["Mae: Out of range"]


def test_rows_keep_the_last_known_state_and_mark_missing_bots():
    samples = [sample(0, [bot("Mae", 0, 0)]), sample(3, [bot("Mae", 0, 0, action="frostbolt")])]
    rows = tl.build_timeline(samples, [], ["Mae", "Gone"])["rows"]
    assert [r["bots"]["Mae"]["action"] for r in rows] == ["check values"] * 3 + ["frostbolt"]
    assert rows[1]["bots"]["Mae"]["moved"] is None  # no new sample in that second
    assert rows[0]["bots"]["Gone"] is None


def test_window_is_cut_to_start_and_end():
    samples = [sample(t, [bot("Mae", 0, 0)]) for t in range(0, 20)]
    out = tl.build_timeline(samples, [], ["Mae"], start=5, end=9)
    assert [r["t"] for r in out["rows"]] == [5, 6, 7, 8, 9]


# ---------------------------------------------------------------- standstill out of range

def test_standstill_from_recorded_repro124_pull1_agrees_with_the_gate():
    s = fixture("r124c_pull1_samples.json")["samples"]
    out = tl.build_timeline(s, [], ["Thormora", "Rarrurdyr", "Sikorstae"])
    runs = [a for a in out["annotations"]["standstill"] if a["bot"] == "Rarrurdyr"]
    gate = bg.evaluate_ranged_reach(s, ["Rarrurdyr"], standstill_s=5)
    assert max(r["seconds"] for r in runs) == gate.metrics["longest_standstill_s"]["Rarrurdyr"] >= 20
    assert all(r["action"] == bg.REACH_SPELL and r["dist_min"] is None for r in runs)  # no unit positions recorded
    assert not [a for a in out["annotations"]["standstill"] if a["bot"] == "Thormora"]  # reach melee, and it moved
    assert out["metrics"]["standstill_s"] == max(a["seconds"] for a in out["annotations"]["standstill"])
    row = next(r for r in out["rows"] if r["t"] == runs[0]["start"] + 1)
    assert "standstill" in row["bots"]["Rarrurdyr"]["notes"]


def test_standstill_needs_five_seconds_still_and_out_of_range():
    def run(dist, seconds, x_step=0.0):
        s = [sample(t, [bot("Mae", t * x_step, 0, action=bg.REACH_SPELL, target="Oracle")],
                    units=[{"name": "Oracle", "x": t * x_step + dist, "y": 0.0}]) for t in range(0, seconds + 1)]
        return tl.build_timeline(s, [], ["Mae"])["annotations"]["standstill"]

    assert [a["seconds"] for a in run(35, 6)] == [6] and run(35, 6)[0]["dist_min"] == 35
    assert run(35, 4) == []           # 4 s: under the 5 s threshold
    assert run(20, 8) == []           # in spell range: the reach action is not the out-of-range case
    assert run(35, 8, x_step=2) == []  # moving


def test_standstill_heal_reach_uses_the_heal_range():
    s = [sample(t, [bot("Lia", 0, 0, action=bg.REACH_HEAL, target="Mae"), bot("Mae", 35, 0)]) for t in range(0, 7)]
    assert tl.build_timeline(s, [], ["Lia", "Mae"])["annotations"]["standstill"] == []  # 35 yd < 40 yd heal range
    s = [sample(t, [bot("Lia", 0, 0, action=bg.REACH_HEAL, target="Mae"), bot("Mae", 45, 0)]) for t in range(0, 7)]
    assert [a["bot"] for a in tl.build_timeline(s, [], ["Lia", "Mae"])["annotations"]["standstill"]] == ["Lia"]


def test_standstill_annotation_is_cut_by_casts():
    def run(cast_at):
        s = [sample(t, [bot("Mae", 0, 0, action=bg.REACH_SPELL, target="Oracle")],
                    units=[{"name": "Oracle", "x": 35.0, "y": 0.0}]) for t in range(0, 13)]
        evs = [ev(t, "spell_go", source="Mae", target="Oracle") for t in cast_at]
        recs = tl.align(tl.records_from_agent_events(evs, ["Mae"]), T0)
        return [(a["start"], a["seconds"]) for a in tl.build_timeline(s, recs, ["Mae"])["annotations"]["standstill"]]

    assert run([]) == [(0, 12)]
    assert run([1.0 + 1.5 * k for k in range(9)]) == []  # a cast every 1.5 s: no 5 s stretch without one
    assert run([0.5]) == [(0.5, 11.5)]  # one cast, then silence: the stand starts at the cast, which is no sample time


# ---------------------------------------------------------------- CC starts vs completions

def test_cc_from_recorded_b11_focus_p1_agrees_with_the_gate():
    d = fixture("b11_p1_moon.json")
    pull = bg.clock_seconds(d["pull"])
    recs = tl.align(tl.records_from_log_lines(d["lines"], d["me"], d["party"]), pull)
    out = tl.build_timeline([], recs, d["party"], start=0)
    cc = out["annotations"]["cc"]
    assert [c["caster"] for c in cc] == ["Maethiza"]
    gate = bg.evaluate_moon_sheep(bg.records_from_lines(d["lines"], d["me"], d["party"]), d["mage"],
                                  {"name": d["moon"]}, pull, within_s=10)
    assert cc[0]["starts"] == gate.metrics["polymorph_starts"]
    assert cc[0]["completions"] == gate.metrics["polymorph_completions"]
    assert cc[0]["first_completion_s"] == gate.metrics["first_completion_s"] == 26.5
    assert cc[0]["first_start_s"] == 10.6
    assert len(cc[0]["unfinished"]) == cc[0]["starts"] - cc[0]["completions"] > 0
    assert out["metrics"]["sheep_s"] == 26.5
    assert out["metrics"]["sheep_wasted_casts"] == cc[0]["starts"] - cc[0]["completions"]
    row = next(r for r in out["rows"] if r["t"] == 10)
    assert "cc start" in row["bots"]["Maethiza"]["notes"]


def test_cc_pairs_starts_with_completions_and_never_counts_a_late_go():
    evs = [ev(1, "spell_start", source="Mae", spell_id=POLY), ev(2.5, "spell_go", source="Mae", target="Oracle", spell_id=POLY),
           ev(4, "spell_start", source="Mae", spell_id=POLY),  # interrupted
           ev(6, "spell_start", source="Mae", spell_id=POLY), ev(7.4, "spell_go", source="Mae", target="Oracle", spell_id=POLY),
           ev(7.5, "aura_applied", target="Oracle", spell_id=POLY, data={"harmful": True})]
    recs = tl.align(tl.records_from_agent_events(evs, ["Mae"]), T0)
    cc = tl.build_timeline([], recs, ["Mae"], start=0, t_pull=0)["annotations"]["cc"][0]
    assert (cc["starts"], cc["completions"], cc["unfinished"], cc["landed"]) == (3, 2, [4.0], 1)
    assert cc["first_start_s"] == 1.0 and cc["first_completion_s"] == 2.5


def test_cc_never_completed_is_reported_as_never():
    recs = tl.align(tl.records_from_agent_events([ev(1, "spell_start", source="Mae", spell_id=POLY)], ["Mae"]), T0)
    out = tl.build_timeline([], recs, ["Mae"], start=0)
    assert out["annotations"]["cc"][0]["first_completion_s"] is None and out["metrics"]["sheep_s"] == "never"
    assert tl.build_timeline([], [], ["Mae"], start=0, end=3)["metrics"]["sheep_s"] is None  # no CC at all


# ---------------------------------------------------------------- first heal latency

def _hp_series(hps, healer_action="renew"):
    return [sample(t, [bot("Lat", 0, 0, hp=hp), bot("Lia", 5, 0, action=healer_action, target="Lat")])
            for t, hp in enumerate(hps)]


def test_first_heal_latency_after_a_drop_below_70():
    s = _hp_series([100, 90, 65, 60, 80, 100, 60, 55, 50])
    evs = [ev(3.5, "heal", source="Lia", target="Lat", spell_id=2050, amount=300),
           ev(0.5, "heal", source="Lia", target="Lat", spell_id=2050, amount=10)]  # before the drop: not a response
    recs = tl.align(tl.records_from_agent_events(evs, ["Lat", "Lia"]), T0)
    out = tl.build_timeline(s, recs, ["Lat", "Lia"])
    heals = out["annotations"]["heals"]
    assert [(h["member"], h["t"], h["hp"], h["latency_s"], h["by"]) for h in heals] == \
        [("Lat", 2, 65, 1.5, "Lia"), ("Lat", 6, 60, None, None)]
    assert out["metrics"]["first_heal_s"] == 1.5 and out["metrics"]["unhealed_drops"] == 1
    assert "below 70%" in out["rows"][2]["bots"]["Lat"]["notes"]


def test_first_heal_counts_a_heal_between_the_last_good_sample_and_the_dashboard_showing_the_drop():
    s = _hp_series([100, 100, 60, 80])
    recs = tl.align(tl.records_from_agent_events([ev(1.6, "heal", source="Lia", target="Lat", amount=200)], ["Lat", "Lia"]), T0)
    assert tl.build_timeline(s, recs, ["Lat", "Lia"])["annotations"]["heals"][0]["latency_s"] == 0.0


def test_first_heal_latency_unknown_without_combat_events():
    out = tl.build_timeline(_hp_series([100, 60, 60]), [], ["Lat", "Lia"])
    assert out["annotations"]["heals"][0]["latency_s"] is None
    assert out["metrics"]["first_heal_s"] is None and out["metrics"]["unhealed_drops"] is None


def test_first_heal_on_the_leader_from_its_status_health():
    lead = lambda hp: {"name": "Harnessb", "map": 70, "x": 0, "y": 0, "health": hp, "max_health": 1000}  # noqa: E731
    s = [sample(0, [bot("Lia", 1, 0)], lead(900)), sample(1, [bot("Lia", 1, 0)], lead(500)),
         sample(2, [bot("Lia", 1, 0)], lead(700))]
    recs = tl.align(tl.records_from_agent_events([ev(1.4, "heal", source="Lia", target="Harnessb", amount=200)], ["Lia"]), T0)
    heals = tl.build_timeline(s, recs, ["Lia"])["annotations"]["heals"]
    assert [(h["member"], h["latency_s"]) for h in heals] == [("Harnessb", 0.4)]


def test_periodic_heal_lines_count_as_heals():
    lines = ["10/12 13:44:53.390  Kaeporric gains 187 health from Niabrenans's Renew.",
             "10/12 13:44:59.453  Latal's Self Healing heals Latal for 93."]
    recs = tl.records_from_log_lines(lines, "Harnessb", ["Latal", "Niabrenans", "Kaeporric"])
    assert [(r["kind"], r["source"], r["target"], r["amount"]) for r in recs] == \
        [("heal", "Niabrenans", "Kaeporric", 187), ("heal", "Latal", "Latal", 93)]


# ---------------------------------------------------------------- deaths

def test_death_with_its_last_5_seconds():
    s = [sample(t, [bot("Mae", 0, 0, hp=max(0, 100 - 12 * t), state="dead" if t >= 9 else "combat",
                        action="frostbolt" if t < 9 else "")]) for t in range(0, 11)]
    evs = [ev(2, "melee", source="Ogre", target="Mae", amount=10, data={"outcome": "normal"}),
           ev(6, "melee", source="Ogre", target="Mae", amount=30, data={"outcome": "normal"}),
           ev(8, "spell_damage", source="Ogre", target="Mae", spell_id=5, amount=40),
           ev(8.5, "death", source="Mae")]
    recs = tl.align(tl.records_from_agent_events(evs, ["Mae"]), T0)
    out = tl.build_timeline(s, recs, ["Mae"])
    deaths = out["annotations"]["deaths"]
    assert len(deaths) == 1 and deaths[0]["name"] == "Mae" and deaths[0]["t"] == 8.5
    assert [l for l in deaths[0]["last_5s"] if "from" in l] == ["+6.0 damage from Ogre 30", "+8.0 damage from Ogre 40"]
    assert any(l.startswith("+4.0 hp 52%") for l in deaths[0]["last_5s"])
    assert out["metrics"]["deaths"] == 1


def test_death_seen_only_on_the_dashboard_and_mob_deaths_ignored():
    s = [sample(t, [bot("Mae", 0, 0, hp=0 if t >= 3 else 50, state="dead" if t >= 3 else "combat")]) for t in range(0, 5)]
    recs = tl.align(tl.records_from_agent_events([ev(1, "death", source="Kobold")], ["Mae"]), T0)
    deaths = tl.build_timeline(s, recs, ["Mae"])["annotations"]["deaths"]
    assert [(d["name"], d["t"], d["seen"]) for d in deaths] == [("Mae", 3, "dashboard")]


# ---------------------------------------------------------------- follow distance and output

def test_follow_distance_to_the_leader():
    lead = {"name": "Harnessb", "map": 70, "x": 0, "y": 0}
    s = [sample(t, [bot("Mae", 40 - 5 * t, 0, state="moving")], dict(lead)) for t in range(0, 7)]
    out = tl.build_timeline(s, [], ["Mae"])
    assert out["annotations"]["follow"] == [{"bot": "Mae", "final_yd": 10.0, "max_yd": 40.0, "mean_yd": 25.0}]
    assert out["metrics"]["follow_yd"] == 10.0


def test_markdown_and_json_outputs():
    s = fixture("r124c_pull1_samples.json")["samples"]
    out = tl.build_timeline(s, [], ["Thormora", "Rarrurdyr", "Sikorstae"], label="r124c", scenario="ranged_reach_in_dungeon")
    md = tl.render_markdown(out)
    assert md.startswith("# Fight timeline r124c")
    assert "## Annotations" in md and "Rarrurdyr stood still" in md
    assert "| t | action (trigger) | moved | dist | hp | mana | caused | took | notes |" in md
    assert "## Rarrurdyr" in md
    back = json.loads(json.dumps(out))
    assert back["kind"] == "fight-timeline" and back["scenario"] == "ranged_reach_in_dungeon"
    assert set(back["metrics"]) >= {"standstill_s", "first_heal_s", "unhealed_drops", "sheep_s",
                                    "sheep_wasted_casts", "follow_yd", "deaths"}


def test_write_files(tmp_path):
    out = tl.build_timeline([sample(0, [bot("Mae", 0, 0)])], [], ["Mae"], label="x1")
    md, js = tl.write(out, str(tmp_path))
    assert os.path.basename(md) == "timeline-x1.md" and os.path.basename(js) == "timeline-x1.json"
    assert json.load(open(js, encoding="utf-8"))["label"] == "x1"


# ---------------------------------------------------------------- the live recording (fakes only)

class FakeClock:
    def __init__(self):
        self.t = T0

    def wall(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeAgent:
    """turtle-agent's RPC surface the recorder uses; the Ogre hits Mae once a second."""

    def __init__(self, clock):
        self.clock, self.calls, self.events = clock, [], []

    def call(self, method, **p):
        self.calls.append((method, p))
        if method == "status":
            return {"character": "Harnessb", "map": 70, "x": 0.0, "y": 0.0, "z": 0.0, "health": 1000,
                    "max_health": 1000, "last_seq": len(self.events)}
        if method == "party":
            return {"members": [{"name": "Mae"}]}
        if method == "units":
            return [{"name": "Ogre", "guid": "0xF1", "x": 35.0, "y": 0.0, "z": 0.0, "dead": False}]
        if method == "gm":
            self.events.append(ev(self.clock.t - T0 + 0.2, "spell_start", source="Mae", spell_id=POLY))
            self.events.append(ev(self.clock.t - T0 + 1.7, "spell_go", source="Mae", target="Ogre", spell_id=POLY))
            return None
        if method == "events":
            since = p["since"]
            page = self.events[since:since + p.get("limit", 5000)]
            return {"events": page, "next": since + len(page), "more": False, "dropped": False}
        raise AssertionError(method)


class FakeDash:
    def __init__(self, clock):
        self.clock = clock

    def bots(self, names=None):
        return [bot("Mae", 0, 0, action=bg.REACH_SPELL, target="Ogre")]


def test_record_fight_with_fakes():
    clock = FakeClock()
    agent = FakeAgent(clock)
    out = tl.record_fight(agent, FakeDash(clock), seconds=9, order=".bot attack Lat", label="fake",
                          wall=clock.wall, sleep=clock.sleep)
    assert ("gm", {"text": ".bot attack Lat"}) in agent.calls
    assert out["bots"] == ["Mae"] and out["me"] == "Harnessb" and out["t_pull"] is not None
    assert len(out["rows"]) == 10
    # Mae's Polymorph lands at 1.7 s, so the 9 s stand is cut there: the 7.3 s after it is the stall
    assert out["annotations"]["standstill"][0]["bot"] == "Mae" and out["annotations"]["standstill"][0]["dist_min"] == 35
    assert out["annotations"]["standstill"][0]["start"] == 1.7
    assert out["annotations"]["cc"][0]["first_completion_s"] == 1.7
    assert out["metrics"]["follow_yd"] == 0.0


def test_moved_looks_back_two_seconds_over_repeated_dashboard_snapshots():
    s = [sample(t, [bot("Lat", x, 0, state="moving")]) for t, x in enumerate([0, 0, 3, 3, 6, 6])]
    rows = tl.build_timeline(s, [], ["Lat"])["rows"]
    assert [r["bots"]["Lat"]["moved"] for r in rows] == [None, False, True, True, True, True]


def test_mcp_fight_timeline_tool_with_fakes(tmp_path):
    from wowharness import mcp_headless
    clock = FakeClock()
    saved = dict(mcp_headless._state)
    try:
        mcp_headless._state.update(client=FakeAgent(clock), dashboard=FakeDash(clock), wall=clock.wall, sleep=clock.sleep)
        out = mcp_headless.fight_timeline(seconds=4, order=".bot attack Lat", label="mcp", out_dir=str(tmp_path))
    finally:
        mcp_headless._state.clear()
        mcp_headless._state.update(saved)
    assert out["ok"] is True and out["markdown"].startswith("# Fight timeline mcp")
    assert out["metrics"]["sheep_s"] == 1.7 and len(out["timeline"]["rows"]) == 5
    assert sorted(os.path.basename(f) for f in out["files"]) == ["timeline-mcp.json", "timeline-mcp.md"]


def test_mcp_fight_timeline_reports_a_missing_dashboard(monkeypatch):
    from wowharness import mcp_headless
    for k in ("BUILDGATE_DASHBOARD_URL", "BUILDGATE_DASHBOARD_USER", "BUILDGATE_DASHBOARD_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setitem(mcp_headless._state, "dashboard_config", os.path.join(HERE, "no-such-settings.json"))
    monkeypatch.setitem(mcp_headless._state, "client", FakeAgent(FakeClock()))
    mcp_headless._state.pop("dashboard", None)
    out = mcp_headless.fight_timeline(seconds=1)
    assert out["ok"] is False and "no dashboard URL" in out["error"]

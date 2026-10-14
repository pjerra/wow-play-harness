"""Build gate: the pass/fail logic of every scenario (recorded runs from 2026-10-12 and fake streams), the
runner and its safety rules against a fake turtle-agent and a fake dashboard, the config, plan and CLI."""
import functools
import importlib.util
import json
import os

import pytest

from buildgate_fakes import POLY_ID, TELE, Clock, FakeServers, FakeWorld
from wowharness import buildgate as bg
from wowharness.dashboard import Dashboard, DashboardUnavailable
from wowharness.headless import HeadlessClient

HERE = os.path.dirname(__file__)
FIX = os.path.join(HERE, "fixtures", "buildgate")
CONFIG = os.path.join(HERE, "..", "tools", "buildgate_uldaman.json")
P4 = ["Latal", "Niabrenans", "Maethiza", "Kaeporric"]


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def bot(name, x, y, map_id=70, state="idle", action="check values", strategy="chat, follow, loot", hp=100):
    return {"name": name, "map": map_id, "x": x, "y": y, "state": state, "last_action": action,
            "strategy": strategy, "hp": hp, "max_hp": 100}


def sample(t, bots, leader=(70, 0.0, 0.0)):
    return {"t": t, "leader": {"map": leader[0], "x": leader[1], "y": leader[2]} if leader else None,
            "bots": {b["name"].lower(): b for b in bots}}


# ---------------------------------------------------------------- standstill_runs

def test_standstill_runs_survive_missing_samples_and_end_on_movement():
    s = [sample(t, [bot("Mae", 0, 0, action="reach spell")]) for t in range(0, 6)]
    s.insert(3, sample(2.5, []))  # dashboard hiccup: no bot in this sample
    s += [sample(6, [bot("Mae", 3, 0, action="reach spell")]), sample(7, [bot("Mae", 3, 0, action="frostbolt")])]
    runs = bg.standstill_runs(s, "Mae", [bg.REACH_SPELL])
    assert [(r["start"], r["end"], r["seconds"]) for r in runs] == [(0, 5, 5), (6, 6, 0)]


# ---------------------------------------------------------------- 1 follow_into_dungeon

def _arrivals(arrive, total=25, strategy="chat, follow, follow jump, loot", combat=()):
    out = []
    for t in range(0, total + 1):
        bots = []
        for n in P4:
            on = t >= arrive.get(n, 999)
            bots.append(bot(n, 1, 1, map_id=70 if on else 0, state="combat" if n in combat and on else "idle",
                            strategy="tank, dps" if n in combat and on else strategy))
        out.append(sample(t, bots))
    return out


def test_follow_into_dungeon_pass_all_four_in_with_follow():
    out = bg.evaluate_follow_into_dungeon(_arrivals({n: 4 for n in P4}), P4, t_tele=0, within_s=20)
    assert out.verdict == bg.PASS
    assert out.metrics["arrival_s"] == {n: 4 for n in P4}


def test_follow_into_dungeon_fail_build11_only_one_came_in():
    out = bg.evaluate_follow_into_dungeon(_arrivals({"Kaeporric": 5}), P4, t_tele=0, within_s=20)
    assert out.verdict == bg.FAIL
    assert sum("not on map 70 within 20 s" in e for e in out.evidence) == 3


def test_follow_into_dungeon_fail_late_arrival_and_missing_follow():
    late = bg.evaluate_follow_into_dungeon(_arrivals({**{n: 3 for n in P4}, "Latal": 22}), P4, 0, within_s=20)
    assert late.verdict == bg.FAIL and any("Latal" in e and "+22.0 s" in e for e in late.evidence)
    # repro-129 side finding: Latal's list was "chat, delayed roll, loot, protection, ..." with no follow
    s = _arrivals({n: 3 for n in P4})
    s[-1]["bots"]["latal"]["strategy"] = "chat, delayed roll, loot, protection, protection raid, quest"
    out = bg.evaluate_follow_into_dungeon(s, P4, 0, within_s=20)
    assert out.verdict == bg.FAIL and any("Latal: no `follow`" in e for e in out.evidence)


def test_follow_into_dungeon_inconclusive_when_only_combat_or_too_short():
    fight = bg.evaluate_follow_into_dungeon(_arrivals({n: 3 for n in P4}, combat=("Latal",)), P4, 0, within_s=20)
    assert fight.verdict == bg.INCONCLUSIVE and any("only combat samples" in e for e in fight.evidence)
    short = bg.evaluate_follow_into_dungeon(_arrivals({"Kaeporric": 3}, total=8), P4, 0, within_s=20)
    assert short.verdict == bg.INCONCLUSIVE


# ---------------------------------------------------------------- 2 stay_then_order_follows

def _stay_run(follows: bool):
    """Build 13 shape: stay, fight at the leader's spot (t 2-8), leader walks 25 yd east at t 12-16."""
    s = []
    for t in range(0, 30):
        lead_x = 0.0 if t < 12 else min(25.0, (t - 12) * 7.0)
        strat = "chat, stay, loot" if (t < 9 or not follows) else "chat, follow, loot"
        x = 2.0 if not follows or t < 13 else max(2.0, lead_x - 2.5)
        st = "combat" if 2 <= t <= 8 else "idle"
        s.append(sample(t, [bot("Kaeporric", x, 0, state=st, strategy="dps" if st == "combat" else strat)], (70, lead_x, 0.0)))
    return s


def test_stay_then_follow_pass_when_follow_returns():
    out = bg.evaluate_stay_then_follow(_stay_run(True), "Kaeporric", t_order=1, t_walk_start=12, t_walk_end=16)
    assert out.verdict == bg.PASS, out.evidence
    assert out.metrics["follow_after"] is True


def test_stay_then_follow_fail_build13_stay_persists():
    out = bg.evaluate_stay_then_follow(_stay_run(False), "Kaeporric", 1, 12, 16)
    assert out.verdict == bg.FAIL
    assert any("no `follow`" in e for e in out.evidence)
    assert any("ended 23.0 yd" in e for e in out.evidence)


def test_stay_then_follow_inconclusive_when_the_fight_outlasts_the_wait():
    # live b13 r8: the bot on stay fought a mob 39 yd away for 25 s+, still in combat when the walk began
    s = _stay_run(False)
    for x in s:
        if x["t"] >= 2:
            x["bots"]["kaeporric"]["state"] = "combat"
    out = bg.evaluate_stay_then_follow(s, "Kaeporric", 1, 12, 16)
    assert out.verdict == bg.INCONCLUSIVE and "still fighting" in out.evidence[0]


def test_stay_then_follow_inconclusive_without_a_fight():
    s = [x for x in _stay_run(True) if True]
    for x in s:
        x["bots"]["kaeporric"]["state"] = "idle"
    out = bg.evaluate_stay_then_follow(s, "Kaeporric", 1, 12, 16)
    assert out.verdict == bg.INCONCLUSIVE


# ---------------------------------------------------------------- 3 healer_follows_ooc

def test_healer_follows_pass():
    s = [sample(t, [bot("Niabrenans", min(45, t * 7) - 3, 0, action="follow")], (70, min(45, t * 7), 0.0))
         for t in range(0, 12)]
    out = bg.evaluate_healer_follows(s, "Niabrenans", t_walk_end=7)
    assert out.verdict == bg.PASS and out.metrics["final_yd"] == 3.0


def test_healer_follows_fail_repro129_frozen_at_38_yd():
    # repro 129: froze at (-209.9,62.5) 37.9 yd from the leader, "reach party member to heal", 70 s+, party 100 %
    s = [sample(t, [bot("Niabrenans", -209.9, 62.5, state="moving", action=bg.REACH_HEAL)], (70, -172.0, 62.3))
         for t in range(0, 71)]
    out = bg.evaluate_healer_follows(s, "Niabrenans", t_walk_end=0)
    assert out.verdict == bg.FAIL
    assert any("stood still 70 s" in e for e in out.evidence)
    assert any("ended 37.9 yd" in e for e in out.evidence)


def test_healer_follows_fail_far_without_stall_and_short_stall_alone_passes():
    far = [sample(t, [bot("Niabrenans", 20 - t, 0, action="follow")], (70, 45.0, 0.0)) for t in range(0, 10)]
    assert bg.evaluate_healer_follows(far, "Niabrenans", 0).verdict == bg.FAIL
    short = [sample(t, [bot("Niabrenans", 0 if t < 15 else 40, 0, action=bg.REACH_HEAL if t < 15 else "follow")],
                    (70, 45.0, 0.0)) for t in range(0, 20)]
    out = bg.evaluate_healer_follows(short, "Niabrenans", 0)
    assert out.verdict == bg.PASS and out.metrics["longest_reach_standstill_s"] == 14


# ---------------------------------------------------------------- 4 ranged_reach_in_dungeon

def test_ranged_reach_fail_recorded_repro124_pull1():
    s = fixture("r124c_pull1_samples.json")["samples"]
    out = bg.evaluate_ranged_reach(s, ["Rarrurdyr"], standstill_s=10)
    assert out.verdict == bg.FAIL
    assert out.metrics["longest_standstill_s"]["Rarrurdyr"] >= 20
    assert any("Rarrurdyr stood still" in e and "reach spell" in e for e in out.evidence)


def test_ranged_reach_pass_moving_casters_and_9s_boundary():
    s = []
    for t in range(0, 30):
        mage_x = 0 if t < 9 else (t - 9) * 3  # 9 s still with reach spell, then closes in
        s.append(sample(t, [bot("Maethiza", mage_x, 0, state="combat", action=bg.REACH_SPELL if t < 9 else "frostbolt"),
                            bot("Niabrenans", t * 0.5, 0, state="combat", action="renew"),
                            bot("Latal", 40, 0, state="combat", action="melee")]))
    out = bg.evaluate_ranged_reach(s, ["Niabrenans", "Maethiza"], standstill_s=10)
    assert out.verdict == bg.PASS and out.metrics["longest_standstill_s"]["Maethiza"] == 8


def test_ranged_reach_inconclusive_when_the_pull_starts_no_fight():
    # repro-124 pull 2: the east pack never engaged; mage and hunter idle on "check values"
    s = [sample(t, [bot("Maethiza", 0, 0), bot("Latal", 5, 0, action="reach melee")]) for t in range(0, 20)]
    assert bg.evaluate_ranged_reach(s, ["Maethiza"]).verdict == bg.INCONCLUSIVE


# ---------------------------------------------------------------- 4b ranged reach: a caster that casts is not stalled

def _held(name, seconds, step=1.0, action=bg.REACH_SPELL):
    """A caster in combat on one spot with `action` for `seconds`, sampled every `step` s."""
    return [sample(round(k * step, 2), [bot(name, 0, 0, state="combat", action=action)])
            for k in range(round(seconds / step) + 1)]


def _cast_records(name, times, damage=True):
    """The agent's events of casts going off at `times` (samples' clock): spell_go, the spell's damage 0.4 s on."""
    evs = []
    for t in times:
        evs.append(ev(t, "spell_go", source=name, target="Stonevault Oracle"))
        if damage:
            evs.append(ev(t + 0.4, "spell_damage", source=name, target="Stonevault Oracle", amount=300))
    return bg.align(bg.records_from_events(evs, P4), T0)


def test_ranged_reach_pass_caster_casting_while_standing_still():  # the audit's mage: 14.5 s, a cast every 1.5 s
    s = _held("Maethiza", 14.5, step=0.5)
    recs = _cast_records("Maethiza", [1.0 + 1.5 * k for k in range(9)])
    out = bg.evaluate_ranged_reach(s, ["Maethiza"], standstill_s=10, records=recs)
    assert out.verdict == bg.PASS and out.metrics["longest_standstill_s"]["Maethiza"] <= 1.5
    assert bg.evaluate_ranged_reach(s, ["Maethiza"], standstill_s=10).verdict == bg.FAIL  # same stand, no cast evidence


def test_ranged_reach_fail_real_stall_no_casts():
    out = bg.evaluate_ranged_reach(_held("Maethiza", 14), ["Maethiza"], standstill_s=10, records=[])
    assert out.verdict == bg.FAIL and out.metrics["longest_standstill_s"]["Maethiza"] == 14
    assert any("Maethiza stood still 14 s" in e and "reach spell" in e for e in out.evidence)


def test_ranged_reach_fail_casts_only_at_the_start_then_silence():
    out = bg.evaluate_ranged_reach(_held("Maethiza", 12), ["Maethiza"], standstill_s=10,
                                   records=_cast_records("Maethiza", [0.5]))
    assert out.verdict == bg.FAIL and out.metrics["longest_standstill_s"]["Maethiza"] >= 11


def test_ranged_reach_spell_start_melee_and_dot_ticks_are_not_casts():
    # a cast that never lands, melee every second, and a DoT tick every 2 s: none of them is a cast
    evs = [ev(k + 0.5, "spell_start", source="Maethiza", target="Stonevault Oracle") for k in range(0, 14, 2)]
    evs += [ev(k + 0.2, "melee", source="Maethiza", target="Stonevault Oracle", amount=60, data={"outcome": "normal"})
            for k in range(14)]
    evs += [ev(k + 0.6, "periodic", source="Maethiza", target="Stonevault Oracle", amount=40, data={"tick": "damage"})
            for k in range(0, 14, 2)]
    recs = bg.align(bg.records_from_events(evs, P4), T0)
    out = bg.evaluate_ranged_reach(_held("Maethiza", 14), ["Maethiza"], standstill_s=10, records=recs)
    assert out.verdict == bg.FAIL and out.metrics["longest_standstill_s"]["Maethiza"] == 14


# ---------------------------------------------------------------- 5 moon_sheep_lands

def test_moon_sheep_fail_recorded_b11_focus_p1():
    d = fixture("b11_p1_moon.json")
    recs = bg.records_from_lines(d["lines"], d["me"], d["party"])
    out = bg.evaluate_moon_sheep(recs, d["mage"], {"name": d["moon"]}, bg.clock_seconds(d["pull"]), within_s=10)
    assert out.verdict == bg.FAIL
    assert out.metrics["first_completion_s"] == 26.5  # b11-focus: "casts Polymorph on Assassin" 13:45:02.5
    assert out.metrics["first_start_s"] == 10.6
    assert out.metrics["polymorph_starts"] > out.metrics["polymorph_completions"]
    assert out.metrics["party_hits_before_sheep"] >= 5
    assert any("Blade Flurry" in e for e in out.evidence)


T0 = 1_791_810_000.0


def ev(dt, kind, **kw):
    from datetime import datetime, timezone
    return {"seq": 0, "time": datetime.fromtimestamp(T0 + dt, timezone.utc).isoformat(), "kind": kind, **kw}


MOON, OTHER = "0xF130001293000101", "0xF130001293000102"


def test_moon_sheep_pass_on_turtle_agent_events_and_guid_matching():
    evs = [ev(0.5, "attack_start", source="Stonevault Rockchewer", source_guid=MOON, target="Latal"),
           ev(1, "spell_start", source="Maethiza", spell_id=POLY_ID, party=True),
           ev(2.5, "spell_go", source="Maethiza", target="Stonevault Rockchewer", target_guid=MOON, spell_id=POLY_ID, party=True),
           ev(2.6, "aura_applied", target="Stonevault Rockchewer", target_guid=MOON, spell_id=POLY_ID, data={"harmful": True}),
           # the same name, another guid: the pulled mob, hit by the tank - not the Moon mob
           ev(2, "melee", source="Latal", target="Stonevault Rockchewer", target_guid=OTHER, amount=60, party=True,
              data={"outcome": "normal"}),
           ev(3, "melee", source="Kaeporric", target="Stonevault Rockchewer", target_guid=MOON, amount=0,
              party=True, data={"outcome": "dodge"})]
    recs = bg.records_from_events(evs, P4)
    out = bg.evaluate_moon_sheep(recs, "Maethiza", {"guid": MOON, "name": "Stonevault Rockchewer"}, T0, 10, True)
    assert out.verdict == bg.PASS, out.evidence
    assert out.metrics == {"polymorph_starts": 1, "polymorph_completions": 1, "completions_on_moon": 1,
                           "first_start_s": 1.0, "first_completion_s": 2.5, "party_hits_before_sheep": 0,
                           "moon_combat_s": 0.5, "first_completion_after_moon_combat_s": 2.0}


def test_moon_sheep_ignores_a_resisted_polymorph_b1():
    # B1: the first Polymorph went off (spell_go) but was resisted; the sheep landed with the second cast
    evs = [ev(0.5, "attack_start", source_guid=MOON, target="Latal"),
           ev(1, "spell_start", source="Maethiza", spell_id=POLY_ID),
           ev(2.5, "spell_go", source="Maethiza", target_guid=MOON, spell_id=POLY_ID),
           ev(2.5, "spell_miss", source="Maethiza", target_guid=MOON, spell_id=POLY_ID, data={"miss": "resist"}),
           ev(5, "spell_start", source="Maethiza", spell_id=POLY_ID),
           ev(6.5, "spell_go", source="Maethiza", target_guid=MOON, spell_id=POLY_ID),
           ev(6.6, "aura_applied", target_guid=MOON, spell_id=POLY_ID, data={"harmful": True})]
    out = bg.evaluate_moon_sheep(bg.records_from_events(evs, P4), "Maethiza", {"guid": MOON}, T0, 10, True)
    assert out.metrics["completions_on_moon"] == 1 and out.metrics["first_completion_s"] == 6.5
    assert out.verdict == bg.PASS, out.evidence
    only_resist = evs[:4]
    out = bg.evaluate_moon_sheep(bg.records_from_events(only_resist, P4), "Maethiza", {"guid": MOON}, T0, 10, True)
    assert out.verdict == bg.FAIL and out.metrics["completions_on_moon"] == 0


def test_moon_sheep_counts_a_sheep_cast_at_the_cc_order_and_matches_the_mage_by_guid():
    # the cc command probes an immediate cast when an enemy is selected: a sheep before the pull is fine
    evs = [ev(0.2, "attack_start", source_guid=MOON, target="Latal"),
           ev(-3, "spell_start", source_guid="0x0000000000001A2B", spell_id=12825),
           ev(-1.5, "spell_go", source_guid="0x0000000000001A2B", target_guid=MOON, spell_id=12825),
           ev(2, "melee", source="Latal", target_guid=OTHER, amount=50, party=True, data={"outcome": "normal"})]
    recs = bg.records_from_events(evs, P4)
    out = bg.evaluate_moon_sheep(recs, "Maethiza", {"guid": MOON}, T0, 10, True, mage_guid=0x1A2B)
    assert out.verdict == bg.PASS and out.metrics["first_completion_s"] == -1.5
    assert any("before the pull" in e for e in out.evidence)
    assert bg.evaluate_moon_sheep(recs, "Maethiza", {"guid": MOON}, T0, 10, True).verdict == bg.FAIL  # no name, no guid


def test_moon_sheep_fail_when_no_cast_completes_or_cleave_lands_first():
    starts = [ev(k, "spell_start", source="Maethiza", spell_id=118) for k in (1, 3, 5)] + \
        [ev(0.5, "attack_start", source_guid=MOON, target="Latal")]
    tank = [ev(2, "melee", source="Latal", target_guid=OTHER, amount=50, party=True, data={"outcome": "normal"})]
    out = bg.evaluate_moon_sheep(bg.records_from_events(starts + tank, P4), "Maethiza", {"guid": MOON}, T0, 10, True)
    assert out.verdict == bg.FAIL and out.metrics["polymorph_starts"] == 3 and out.metrics["polymorph_completions"] == 0
    cleave = [ev(0.5, "attack_start", source_guid=MOON, target="Latal"),
              ev(1.5, "spell_damage", source="Latal", target_guid=MOON, spell_id=20569, amount=61, party=True),
              ev(2.5, "spell_go", source="Maethiza", target_guid=MOON, spell_id=118)]
    out = bg.evaluate_moon_sheep(bg.records_from_events(cleave, P4), "Maethiza", {"guid": MOON}, T0, 10, True)
    assert out.verdict == bg.FAIL and out.metrics["party_hits_before_sheep"] == 1


def test_moon_sheep_inconclusive_setups():
    pre = [ev(-5, "melee", source="Kaeporric", target_guid=MOON, amount=80, party=True, data={"outcome": "normal"}),
           ev(2, "spell_go", source="Maethiza", target_guid=MOON, spell_id=118)]
    assert bg.evaluate_moon_sheep(bg.records_from_events(pre, P4), "Maethiza", {"guid": MOON}, T0).verdict == bg.INCONCLUSIVE
    assert bg.evaluate_moon_sheep([], "Maethiza", {"guid": MOON}, T0).verdict == bg.INCONCLUSIVE
    tank_only = [ev(2, "melee", source="Latal", target_guid=OTHER, amount=50, party=True, data={"outcome": "normal"})]
    out = bg.evaluate_moon_sheep(bg.records_from_events(tank_only, P4), "Maethiza", {"guid": MOON}, T0, 10, moon_engaged=False)
    assert out.verdict == bg.INCONCLUSIVE


def test_moon_sheep_ten_seconds_count_from_the_moon_mobs_own_combat_entry():
    # b16 run 2: the Moon mob joined 9.0 s after the pull order, the sheep landed 13.3 s after it (4.3 s after)
    evs = [ev(9.0, "attack_start", source="Stonevault Oracle", source_guid=MOON, target="Latal"),
           ev(11, "spell_start", source="Maethiza", spell_id=POLY_ID, party=True),
           ev(13.3, "spell_go", source="Maethiza", target_guid=MOON, spell_id=POLY_ID, party=True)]
    out = bg.evaluate_moon_sheep(bg.records_from_events(evs, P4), "Maethiza", {"guid": MOON}, T0, 10, True)
    assert out.verdict == bg.PASS, out.evidence
    assert out.metrics["first_completion_s"] == 13.3  # the pull-based figure is kept as information
    assert out.metrics["moon_combat_s"] == 9.0 and out.metrics["first_completion_after_moon_combat_s"] == 4.3
    # too slow measured from its own combat entry: 11.5 s after it joined
    evs[2] = ev(20.5, "spell_go", source="Maethiza", target_guid=MOON, spell_id=POLY_ID, party=True)
    out = bg.evaluate_moon_sheep(bg.records_from_events(evs, P4), "Maethiza", {"guid": MOON}, T0, 10, True)
    assert out.verdict == bg.FAIL and any("11.5 s after it entered combat" in e for e in out.evidence)


def test_moon_sheep_anchor_is_the_first_event_or_the_first_poll_whichever_is_earlier():
    late_go = [ev(12, "spell_go", source="Maethiza", target_guid=MOON, spell_id=POLY_ID, party=True),
               ev(11, "melee", source="Latal", target_guid=OTHER, amount=50, party=True, data={"outcome": "normal"})]
    recs = bg.records_from_events(late_go, P4)
    # no event with the Moon mob in it: only the poll saw it fight, at +3 s
    out = bg.evaluate_moon_sheep(recs, "Maethiza", {"guid": MOON}, T0, 10, True, moon_combat_t=T0 + 3)
    assert out.verdict == bg.PASS and out.metrics["moon_combat_s"] == 3.0
    assert out.metrics["first_completion_after_moon_combat_s"] == 9.0
    # a Polymorph record is not a combat entry
    assert bg.evaluate_moon_sheep(recs, "Maethiza", {"guid": MOON}, T0, 10, True).verdict == bg.INCONCLUSIVE
    # the poll came later than the event: the event wins
    ev2 = late_go + [ev(1, "attack_start", source_guid=MOON, target="Latal")]
    out = bg.evaluate_moon_sheep(bg.records_from_events(ev2, P4), "Maethiza", {"guid": MOON}, T0, 10, True,
                                 moon_combat_t=T0 + 5)
    assert out.verdict == bg.FAIL and out.metrics["moon_combat_s"] == 1.0


def test_moon_sheep_inconclusive_when_the_moon_mob_never_enters_combat():
    evs = [ev(2, "melee", source="Latal", target_guid=OTHER, amount=50, party=True, data={"outcome": "normal"}),
           ev(3, "spell_go", source="Maethiza", target_guid=MOON, spell_id=POLY_ID, party=True)]
    out = bg.evaluate_moon_sheep(bg.records_from_events(evs, P4), "Maethiza", {"guid": MOON}, T0, 10, True)
    assert out.verdict == bg.INCONCLUSIVE and "never entered combat" in out.evidence[0]


def test_not_full_wants_hp_and_mana_and_treats_a_missing_figure_as_full():
    assert not bg._not_full({"hp": 10, "max_hp": 10, "power": 5, "max_power": 5, "power_type": "mana"})
    assert bg._not_full({"hp": 9, "max_hp": 10})
    assert bg._not_full({"hp": 10, "max_hp": 10, "power": 4, "max_power": 5, "power_type": "mana"})
    assert not bg._not_full({"hp": 10, "max_hp": 10, "power": 0, "max_power": 100, "power_type": "rage"})
    assert not bg._not_full({"name": "x"})


def test_records_from_events_counts_only_landed_damage():
    evs = [ev(1, "melee", source="Latal", target_guid=MOON, data={"outcome": "parry"}),
           ev(2, "periodic", source="Niabrenans", target_guid=MOON, amount=30, data={"tick": "heal"}),
           ev(3, "periodic", source="Niabrenans", target_guid=MOON, amount=104, data={"tick": "damage"})]
    recs = bg.records_from_events(evs, P4)
    assert [(r["kind"], r["party"]) for r in recs] == [("damage", True)]


# ---------------------------------------------------------------- 6 no_jumps_in_dungeon

def test_no_jumps_recorded_vm_samples():
    s = fixture("ulda_vm_samples.json")["samples"]
    inside = bg.evaluate_no_jumps(s, map_id=70)
    assert inside.verdict == bg.PASS and inside.metrics["dungeon_bot_rows"] >= 100
    outside = bg.evaluate_no_jumps(s, map_id=0)  # the old code's combat jumps happened outside, on map 0
    assert outside.verdict == bg.FAIL and outside.metrics["jumps"] == 7


def test_no_jumps_inconclusive_with_few_samples():
    s = [sample(t, [bot("Latal", 0, 0)]) for t in range(3)]
    assert bg.evaluate_no_jumps(s, 70, min_samples=10).verdict == bg.INCONCLUSIVE


# ---------------------------------------------------------------- config, plan, CLI

def load_cfg():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def test_shipped_config_loads_and_keeps_the_time_limits():
    cfg = bg.load_gate_config(CONFIG)
    assert cfg["dungeon"]["map"] == 70 and cfg["safe"]["tele"] == "ulda"
    assert all(cfg["scenarios"][n]["budget_s"] <= 180 for n in bg.ORDER)
    assert sum(cfg["scenarios"][n]["budget_s"] for n in bg.ORDER) <= cfg["budget_s"] == 2040
    hs = cfg["scenarios"]["healer_follows_ooc"]
    walk = bg.dist2d(hs["start_spot"], hs["end_spot"])
    assert 44 <= walk <= 46  # "walk 45 yd in a straight line"
    st = cfg["scenarios"]["stay_then_order_follows"]
    assert 24 <= bg.dist2d(st["spot"], st["walk_to"]) <= 26
    rr = cfg["scenarios"]["ranged_reach_in_dungeon"]
    assert 35 <= bg.dist2d(rr["spot"], rr["pull_at"]) <= 42  # repro-124: 35-40 yd pulls (pack centre)


@pytest.mark.parametrize("mutate, msg", [
    (lambda c: c.__setitem__("dashboard_password", "x"), "no credentials"),
    (lambda c: c["scenarios"]["moon_sheep_lands"].__setitem__("password", "x"), "no credentials"),
    (lambda c: c["scenarios"]["healer_follows_ooc"].__setitem__("end_spot", {"x": 1, "y": 2}), "numeric x, y, z"),
    (lambda c: c["scenarios"]["stay_then_order_follows"].__setitem__("bot", "hunter"), "not a party key"),
    (lambda c: c["scenarios"]["moon_sheep_lands"].__setitem__("budget_s", 400), "under ~3 minutes"),
    (lambda c: c["scenarios"]["no_jumps_in_dungeon"].pop("route"), "route is required"),
    (lambda c: c.__setitem__("safe", {"tele": "ulda; .gm off"}), "bad tele name"),
])
def test_config_rejects(mutate, msg):
    raw = load_cfg()
    mutate(raw)
    with pytest.raises(ValueError, match=msg):
        bg.check_gate_config(raw)


def _cli():
    spec = importlib.util.spec_from_file_location("buildgate_cli", os.path.join(HERE, "..", "tools", "buildgate.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_dry_run_prints_the_plan_without_network(capsys):
    assert _cli().main(["--config", CONFIG, "--dry-run"]) == 0
    out = capsys.readouterr().out
    for n in bg.ORDER:
        assert f"[{n}]" in out
    assert "logout (refused: .gm on, wait, retry once, report)" in out
    # each scenario's setup has its own cap (180 s) on top of its budget, and the plan counts it
    assert "worst case 2040 s of 2040 s gate budget (960 s of scenario budgets + 6 x 180 s setup)" in out
    assert "the gate budget ends the run first" not in out  # 2040 s covers the worst case
    assert "[follow_into_dungeon] setup <= 180 s, then budget 180 s" in out


def test_cli_rejects_unknown_scenario(capsys):
    assert _cli().main(["--config", CONFIG, "--scenarios", "fly_to_moon", "--dry-run"]) == 3


def test_dashboard_settings_read_only_dashboard_keys(tmp_path, monkeypatch):
    for k in ("URL", "USER", "PASSWORD"):
        monkeypatch.delenv(f"BUILDGATE_DASHBOARD_{k}", raising=False)
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"dashboard_url": "http://d:8095", "dashboard_user": "gm", "dashboard_password": "pw",
                             "account": "ACC", "password": "GAMEPW"}))
    assert _cli().dashboard_settings(None, str(p)) == ("http://d:8095", "gm", "pw")
    monkeypatch.setenv("BUILDGATE_DASHBOARD_URL", "http://env:1")
    assert _cli().dashboard_settings(None, str(p))[0] == "http://env:1"


# ---------------------------------------------------------------- the runner on the fake world

@pytest.fixture
def world_factory():
    made = []

    def make(bugs=(), config_patch=None):
        clock = Clock()
        world = FakeWorld(clock, bugs)
        servers = FakeServers(world)
        made.append(servers)
        world.servers = servers
        raw = load_cfg()
        if config_patch:
            config_patch(raw)
        cfg = bg.check_gate_config(raw)
        gate = bg.Gate(HeadlessClient(servers.rpc_addr, timeout_s=5), Dashboard(servers.dash_url, "u", "p"), cfg,
                       clock=clock.now, sleep=clock.sleep, wall=clock.now)
        return world, gate, cfg
    yield make
    for s in made:
        s.close()


def _gm_texts(world):
    return [p["text"] for m, p in world.calls if m == "gm"]


def test_gate_good_build_passes_everything_and_logs_out_safely(world_factory):
    world, gate, cfg = world_factory()
    rep = gate.run(bg.ORDER)
    verdicts = {r["name"]: r["verdict"] for r in rep["scenarios"]}
    assert verdicts == {n: bg.PASS for n in bg.ORDER}, json.dumps(rep["scenarios"], indent=1)
    assert rep["verdict"] == bg.PASS
    assert rep["teardown"]["result"] == "logged out"
    moon = next(r for r in rep["scenarios"] if r["name"] == "moon_sheep_lands")
    assert moon["metrics"]["first_completion_s"] <= 10
    assert any("TBM:ACTION_ACK|cc moon" in e for e in moon["evidence"])
    # each scenario stayed within its budget, the gate within 20 min of game time
    assert all(r["seconds"] <= cfg["scenarios"][r["name"]]["budget_s"] for r in rep["scenarios"])
    assert rep["seconds"] <= 1200
    # safety: every .gm off (the fight scenarios run with GM off) comes right after .gm on + the safe spot;
    # teardown order stop_attack -> leave_group -> .gm on -> logout
    gms = _gm_texts(world)
    offs = [i for i, t in enumerate(gms) if t == ".gm off"]
    assert len(offs) == 2 and all(gms[i - 2:i] == [".gm on", ".tele ulda"] for i in offs)
    tail = [(m, p.get("text")) for m, p in world.calls if m in ("stop_attack", "leave_group", "logout") or
            (m == "gm" and p.get("text") == ".gm on")]
    assert tail[-4:] == [("stop_attack", None), ("leave_group", None), ("gm", ".gm on"), ("logout", None)]
    # the hostile packs the fights used are dead (the friendly Rockchewer is left alone), and no .die was needed
    assert all(c["dead"] or c.get("faction") == 35 for c in world.creatures.values())
    assert ".die" not in gms
    md = bg.render_markdown(rep)
    assert "verdict: **PASS**" in md and "## moon_sheep_lands: PASS" in md


@pytest.mark.parametrize("bug, scenario, needle", [
    ("no_cross_map_follow", "follow_into_dungeon", "not on map 70 within 20 s"),
    ("stay_sticks", "stay_then_order_follows", "no `follow`"),
    ("healer_stall", "healer_follows_ooc", "reach party member to heal"),
    ("caster_stall", "ranged_reach_in_dungeon", "with 'reach spell'"),
    ("late_sheep", "moon_sheep_lands", "party hit(s) on the Moon mob"),
    ("jump", "no_jumps_in_dungeon", "'jump'"),
])
def test_gate_catches_each_of_tonights_bugs(world_factory, bug, scenario, needle):
    world, gate, cfg = world_factory(bugs=[bug])
    names = [scenario] if scenario == "follow_into_dungeon" else ["follow_into_dungeon", scenario] \
        if bug != "no_cross_map_follow" else [scenario]
    if bug == "jump":  # jumping bots fail the dungeon walk; the follow check just brings them in
        names = ["follow_into_dungeon", "no_jumps_in_dungeon"]
    rep = gate.run(names, logout=False)
    r = next(x for x in rep["scenarios"] if x["name"] == scenario)
    assert r["verdict"] == bg.FAIL, r
    assert any(needle in e for e in r["evidence"]), r["evidence"]


def test_stay_reply_that_reaches_the_event_log_late_is_still_read(world_factory):
    # live b18: the gate's one read 4 s after `stay` found no reply ("no whisper reply"); the reply can arrive more than
    # 4 s after the order
    world, gate, cfg = world_factory(bugs=["reply_lag"])
    r = gate.run(["stay_then_order_follows"], logout=False)["scenarios"][0]
    assert any(e == "after `stay`: Kaeporric replied 'Staying'" for e in r["evidence"]), r["evidence"]


def test_stay_with_no_reply_says_so_and_the_scenario_goes_on(world_factory):
    world, gate, cfg = world_factory(bugs=["reply_none"])
    r = gate.run(["stay_then_order_follows"], logout=False)["scenarios"][0]
    assert any(e == "after `stay`: no whisper reply from Kaeporric" for e in r["evidence"]), r["evidence"]
    assert any(e.startswith("attack target:") for e in r["evidence"]), r["evidence"]  # the scenario went on


def test_gate_ranged_casters_that_keep_casting_from_their_stand_pass(world_factory):
    # repro 124 with the casters still casting: a spell goes off every 1.5 s while they stand on "reach spell"
    world, gate, cfg = world_factory(bugs=["caster_casts_from_range"])
    rep = gate.run(["follow_into_dungeon", "ranged_reach_in_dungeon"], logout=False)
    r = next(x for x in rep["scenarios"] if x["name"] == "ranged_reach_in_dungeon")
    assert r["verdict"] == bg.PASS, r["evidence"]


def test_gate_moon_late_sheep_reports_starts_vs_completions(world_factory):
    world, gate, cfg = world_factory(bugs=["late_sheep"])
    rep = gate.run(["moon_sheep_lands"], logout=False)
    r = rep["scenarios"][0]
    assert r["verdict"] == bg.FAIL
    assert r["metrics"]["polymorph_starts"] == 3 and r["metrics"]["polymorph_completions"] == 1
    assert ".bot action cc clear Maethiza" in _gm_texts(world)  # the assignment is always cleared


def test_gate_moon_waits_for_the_mage_to_finish_eating_before_order_and_pull(world_factory):
    world, gate, cfg = world_factory(bugs=["mage_eats"])
    rep = gate.run(["moon_sheep_lands"], logout=False)
    r = rep["scenarios"][0]
    assert r["verdict"] == bg.PASS, r
    assert world.eating_at_order == [] and world.eating_at_pull == []


def test_gate_moon_rechecks_rested_right_before_the_pull(world_factory):
    world, gate, cfg = world_factory(bugs=["mage_eats_after_order"])
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r
    assert world.eating_at_pull == []


def test_gate_moon_reports_a_party_that_never_rests_in_the_setup_error(world_factory):
    world, gate, cfg = world_factory(bugs=["mage_eats_forever"],
                                     config_patch=lambda c: c["scenarios"]["moon_sheep_lands"].update(rest_wait_s=5))
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.INCONCLUSIVE, r
    # the scenario's ready check (before the scenario starts) catches it first; the cc order is never sent
    assert any("not rested" in e and "before the scenario starts" in e and "Maethiza" in e for e in r["evidence"]), r["evidence"]
    assert world.eating_at_order is None  # the order was never sent


def test_gate_moon_counts_the_ten_seconds_from_the_moon_mobs_combat(world_factory):
    world, gate, cfg = world_factory(bugs=["moon_joins_late"])
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r
    assert r["metrics"]["first_completion_s"] > 10  # 12 s after the pull order, kept as information
    assert 3 <= r["metrics"]["first_completion_after_moon_combat_s"] <= 5


def test_gate_moon_inconclusive_when_the_moon_mob_never_fights(world_factory):
    world, gate, cfg = world_factory(bugs=["moon_never_fights"])
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.INCONCLUSIVE, r


PACK_AT = {"x": -168.7, "y": 70.7, "z": -48.3, "map": 70}
HALL_ORACLES = [4852, 4852]


def _kill_hall_oracles(world):
    """The Hall of the Keepers Oracles die now (as the stay scenario's fight kills the pack)."""
    world.leader.update(map=70, x=PACK_AT["x"], y=PACK_AT["y"], z=PACK_AT["z"])
    gs = [g for g, c in world.creatures.items() if c["entry"] == 4852 and abs(c["x"] - PACK_AT["x"]) < 12]
    assert len(gs) == 2
    world._kill(gs)
    return gs


def test_prepare_pack_respawns_a_pack_that_died_but_is_still_listed_alive(world_factory):
    # live runs 1 and 3: ranged_reach's pack had just been killed, the units list still showed it alive
    world, gate, cfg = world_factory(bugs=["dead_lag"])
    gs = _kill_hall_oracles(world)
    assert not any(u.get("dead") for u in gate.units(entry=4852, near=150))  # the list says alive
    pack = gate.prepare_pack(PACK_AT, HALL_ORACLES)
    assert ".respawn" in _gm_texts(world)
    assert sorted(u["guid"] for u in pack) == sorted(gs)
    assert all(not world.creatures[u["guid"]]["dead"] for u in pack)


def test_prepare_pack_does_not_respawn_a_pack_that_is_alive(world_factory):
    world, gate, cfg = world_factory(bugs=["dead_lag"])
    world.leader.update(map=70, x=PACK_AT["x"], y=PACK_AT["y"], z=PACK_AT["z"])
    pack = gate.prepare_pack(PACK_AT, HALL_ORACLES)
    assert len(pack) == 2 and ".respawn" not in _gm_texts(world)


def test_find_pack_reads_zero_hp_as_dead(world_factory):
    world, gate, cfg = world_factory()
    world.leader.update(map=70, x=PACK_AT["x"], y=PACK_AT["y"], z=PACK_AT["z"])
    g = next(g for g, c in world.creatures.items() if c["entry"] == 4852 and abs(c["x"] - PACK_AT["x"]) < 12)
    world.creatures[g]["hp"] = 0  # the dead flag not set yet
    found = gate.find_pack(PACK_AT, HALL_ORACLES)
    assert None in found and g not in [u["guid"] for u in found if u]


def test_prepare_pack_waits_for_a_slow_respawn_to_be_listed_alive(world_factory):
    # live run 3: "no live pack [4852,4852] after .respawn" two seconds after the command
    world, gate, cfg = world_factory(bugs=["respawn_lag"])
    gs = _kill_hall_oracles(world)
    t0 = world.now()
    pack = gate.prepare_pack(PACK_AT, HALL_ORACLES)
    assert sorted(u["guid"] for u in pack) == sorted(gs)
    assert 5 <= world.now() - t0 <= 20


def test_prepare_pack_gives_up_after_a_bounded_wait_when_nothing_respawns(world_factory):
    world, gate, cfg = world_factory(bugs=["respawn_never"])
    _kill_hall_oracles(world)
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match=r"no live pack .* after \.respawn \(waited 12 s"):
        gate.prepare_pack(PACK_AT, HALL_ORACLES)
    assert world.now() - t0 <= bg.PACK_RESPAWN_WAIT_S + bg.PACK_CONFIRM_S + 6


def _moon_marks(world):
    return [(e["target_guid"], e["data"]["icon"]) for e in world.events
            if e["kind"] == "raid_mark" and e["data"]["action"] == "set"]


def _rockchewer(world):
    return next(g for g, c in world.creatures.items() if c["entry"] == 4851)


def test_gate_moon_mark_on_the_stale_selection_is_resent_and_lands(world_factory):
    # live run 3: the chat command ran before the selection packet and the moon mark landed on a dead mob
    world, gate, cfg = world_factory()
    world.cc_race, world.cc_race_guid = 1, _rockchewer(world)
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r
    marks = _moon_marks(world)
    assert len(marks) == 2 and marks[0][0] == world.cc_race_guid and marks[1][0] != world.cc_race_guid
    assert sum(t == ".bot action cc moon Maethiza" for t in _gm_texts(world)) == 2
    assert any("selecting" in e and "again" in e and "landed on" in e for e in r["evidence"]), r["evidence"]
    assert any(e.endswith("cc moon: resending") for e in r["evidence"]), r["evidence"]


def test_gate_moon_mark_that_misses_twice_is_inconclusive_with_the_evidence(world_factory):
    world, gate, cfg = world_factory()
    world.cc_race, world.cc_race_guid = 2, _rockchewer(world)
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.INCONCLUSIVE, r
    assert any("order missed" in e and "twice" in e and "mark landed on" in e and world.cc_race_guid in e
               for e in r["evidence"]), r["evidence"]
    assert len(_moon_marks(world)) == 1  # the icon stayed on the wrong unit: the second order logged nothing
    assert sum(t == ".bot action cc moon Maethiza" for t in _gm_texts(world)) == 2
    assert ".bot action cc clear Maethiza" in _gm_texts(world)  # still cleared


def test_gate_moon_with_no_raid_mark_event_is_resent_once_then_inconclusive(world_factory):
    world, gate, cfg = world_factory()
    real = world.emit
    world.emit = lambda kind, **kw: None if kind == "raid_mark" else real(kind, **kw)
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.INCONCLUSIVE, r
    assert any("no raid_mark event for the moon icon" in e for e in r["evidence"]), r["evidence"]
    assert sum(t == ".bot action cc moon Maethiza" for t in _gm_texts(world)) == 2


def test_cc_ack_that_reaches_the_event_log_late_is_still_read(world_factory):
    # run 2: "no TBM:ACTION line seen" though TBM:ACTION_ACK|cc moon arrived a few seconds later
    world, gate, cfg = world_factory(bugs=["ack_lag"])
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r
    assert any(e.startswith("cc order reply: TBM:ACTION_ACK|cc moon") for e in r["evidence"]), r["evidence"]


def test_cc_ack_that_never_comes_says_so_after_the_bounded_wait(world_factory):
    world, gate, cfg = world_factory()
    real = world.emit
    world.emit = lambda kind, **kw: None if "TBM:ACTION" in kw.get("text", "") else real(kind, **kw)
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert any(e == "cc order reply: (no TBM:ACTION line seen)" for e in r["evidence"]), r["evidence"]


def test_gate_moon_mark_already_on_the_moon_mob_passes_without_a_resend(world_factory):
    # turtle-agent logs a raid_mark only when the icon's holder changes; `cc clear` leaves the group icon where it is,
    # so a rerun's order on the same mob logs nothing
    world, gate, cfg = world_factory()
    assert gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]["verdict"] == bg.PASS
    moon = world.marks["moon"]
    n_events, n_orders = len(_moon_marks(world)), sum(t.startswith(".bot action cc moon") for t in _gm_texts(world))
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r
    assert world.marks["moon"] == moon and len(_moon_marks(world)) == n_events  # no new event
    assert sum(t.startswith(".bot action cc moon") for t in _gm_texts(world)) == n_orders + 1  # one order, no resend


def test_gate_moon_mark_already_on_the_moon_mob_but_the_order_acts_on_a_stale_selection_is_resent(world_factory):
    # cleared(Moon) + set(dead mob): the cleared row must not count as a landing
    world, gate, cfg = world_factory()
    assert gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]["verdict"] == bg.PASS
    moon = world.marks["moon"]
    world.cc_race, world.cc_race_guid = 1, _rockchewer(world)
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r
    rows = [(e["data"]["action"], e["target_guid"]) for e in world.events if e["kind"] == "raid_mark"][-4:]
    assert rows == [("cleared", moon), ("set", world.cc_race_guid), ("cleared", world.cc_race_guid), ("set", moon)]
    assert any("resending" in e for e in r["evidence"]), r["evidence"]


def _dead_mob(world):
    g = world._spawn(99, world.leader["map"], world.leader["x"] + 300, world.leader["y"], world.leader["z"])
    world.creatures[g].update(dead=True, hp=0)
    return g


def test_gate_moon_order_refused_on_a_stale_dead_selection_while_the_icon_holds_moon_is_resent(world_factory):
    # TortoiseBots #841: a cc order on a dead selection is refused (TBM:ACTION_ERR|cc moon <mage>|no-target|...), moves no mark
    # and saves no assignment; the icon still on the Moon mob from the last run must not count as a landing
    world, gate, cfg = world_factory()
    assert gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]["verdict"] == bg.PASS
    moon, n_events = world.marks["moon"], len(_moon_marks(world))
    world.cc_race, world.cc_race_guid = 1, _dead_mob(world)
    seen = []
    real = world.rpc_gm
    world.rpc_gm = lambda p: (real(p), seen.append(dict(world.cc)) if p["text"].startswith(".bot action cc moon") else None)[0]
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r
    assert world.marks["moon"] == moon and len(_moon_marks(world)) == n_events  # neither order moved the icon
    assert seen == [{}, {"moon": ("Maethiza", moon)}]  # refused, then the assignment on the Moon mob
    assert any("refused" in e and "TBM:ACTION_ERR|cc moon Maethiza|no-target" in e for e in r["evidence"]), r["evidence"]
    assert any(e.endswith("cc moon: resending") for e in r["evidence"]), r["evidence"]
    assert any(e.startswith("cc order reply: TBM:ACTION_ACK|cc moon") for e in r["evidence"]), r["evidence"]


def test_gate_moon_order_refused_twice_is_inconclusive_with_the_err_text(world_factory):
    world, gate, cfg = world_factory()
    assert gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]["verdict"] == bg.PASS
    world.cc_race, world.cc_race_guid = 2, _dead_mob(world)
    n_orders = sum(t == ".bot action cc moon Maethiza" for t in _gm_texts(world))
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.INCONCLUSIVE, r
    assert any("order missed" in e and "twice" in e and e.count("TBM:ACTION_ERR|cc moon Maethiza|no-target") == 2
               for e in r["evidence"]), r["evidence"]
    assert sum(t == ".bot action cc moon Maethiza" for t in _gm_texts(world)) == n_orders + 2


def test_gate_moon_order_refused_after_the_icon_reached_the_moon_mob_is_a_miss(world_factory):
    # the server sets the icon before it checks the executor: set(Moon) + ERR no-cc means no assignment, the ERR wins
    world, gate, cfg = world_factory(bugs=["cc_no_cc"])
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.INCONCLUSIVE, r
    assert any("twice" in e and "TBM:ACTION_ERR|cc moon Maethiza|no-cc" in e for e in r["evidence"]), r["evidence"]
    assert sum(t == ".bot action cc moon Maethiza" for t in _gm_texts(world)) == 2  # no "landed late" shortcut


def test_cc_reply_of_matches_only_this_order():
    texts = ["TBM:ACTION_ACK|cc skull Maethiza|bot:Maethiza|1|Maethiza", "TBM:ACTION_ACK|cc moon Other|bot:Other|1|Other",
             "TBM:ACTION_ACK|cc moon|bot:Other|1|Other", "TBM:ACTION_ACK|attack|party|3|-",
             "TBM:ACTION_ERR|cc moon Other|no-target|x", "Bot Maethiza will attack your selected target."]
    assert bg.Gate.cc_reply_of(texts, "moon", "Maethiza") is None
    ack = "TBM:ACTION_ACK|cc moon Maethiza|bot:Maethiza|1|Maethiza"
    assert bg.Gate.cc_reply_of(texts + [ack], "moon", "Maethiza") == ack
    assert bg.Gate.cc_reply_of(["TBM:ACTION_ACK|cc moon|bot:Maethiza|1|Maethiza"], "moon", "Maethiza")  # no bot named


def test_cc_reply_of_reads_the_real_server_lines():
    # verbatim from the live gate run (~/TortoiseBots-audit/tests/gate-p2-haiku-max/timeline.md:461)
    live = "TBM:ACTION_ACK|cc moon Khikraco|bot:Khikraco|1|Khikraco"
    assert bg.Gate.cc_reply_of([live], "moon", "Khikraco") == live
    assert bg.Gate.cc_reply_of([live], "moon", "khikraco") == live  # the order's case need not match
    assert bg.Gate.cc_reply_of([live], "moon", "Maethiza") is None
    assert bg.Gate.cc_reply_of([live], "skull", "Khikraco") is None
    # SendActionError's shape: TBM:ACTION_ERR|<intent>|<code>|<message>
    err = "TBM:ACTION_ERR|cc moon Khikraco|no-target|Select an enemy target or an owned bot with an existing raid mark."
    assert bg.Gate.cc_reply_of(["Khikraco whispers: hi", err], "moon", "Khikraco") == err
    assert bg.Gate.cc_reply_of([err], "moon", "Maethiza") is None


def test_gate_moon_mark_that_reaches_the_right_unit_late_is_not_resent(world_factory):
    world, gate, cfg = world_factory()
    world.mark_delay = 4.3  # past MARK_WAIT_S, before the re-selection is over
    r = gate.run(["moon_sheep_lands"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r
    assert sum(t.startswith(".bot action cc moon") for t in _gm_texts(world)) == 1
    assert any("reached" in e and "late; not resent" in e for e in r["evidence"]), r["evidence"]


def test_acks_from_leaves_out_the_ack_of_an_earlier_order(world_factory):
    world, gate, cfg = world_factory()
    world.emit("system", text="TBM:ACTION_ACK|cc moon Maethiza|bot:Maethiza|1|Maethiza", data={"type": "system"})
    seq = gate.last_seq()
    assert gate.acks_from(seq, gate.now()) == []
    world.emit("system", text="TBM:ACTION_ACK|cc moon Maethiza|bot:Maethiza|2|Maethiza", data={"type": "system"})
    assert gate.acks_from(seq, gate.now()) == ["TBM:ACTION_ACK|cc moon Maethiza|bot:Maethiza|2|Maethiza"]


def test_logout_refused_once_gets_gm_on_wait_and_one_retry(world_factory):
    world, gate, cfg = world_factory(bugs=["logout_refuse_once"])
    t0 = world.now()
    td = gate.teardown(logout=True)
    assert td["result"] == "logged out on retry"
    seq = [(m, p.get("text")) for m, p in world.calls if m in ("logout", "gm")]
    assert seq[-3:] == [("logout", None), ("gm", ".gm on"), ("logout", None)]
    assert world.now() - t0 >= cfg["logout_retry_wait_s"]


def test_logout_refused_twice_is_reported_not_forced(world_factory):
    world, gate, cfg = world_factory(bugs=["logout_refuse"])
    td = gate.teardown(logout=True)
    assert td["result"] == "LOGOUT REFUSED"
    assert sum(1 for m, _ in world.calls if m == "logout") == 2
    assert any(s.startswith("logout retry: ") and "refused" in s for s in td["steps"])


def test_logout_ended_by_the_server_is_not_retried(world_factory):
    world, gate, cfg = world_factory(bugs=["logout_eof"])
    td = gate.teardown(logout=True)
    assert td["result"].startswith("session closed by the server")
    assert sum(1 for m, _ in world.calls if m == "logout") == 1


def test_gm_off_always_goes_to_the_safe_spot_with_gm_on_first(world_factory):
    def patch(raw):
        raw["scenarios"]["healer_follows_ooc"]["gm_off"] = True
    world, gate, cfg = world_factory(config_patch=patch)
    rep = gate.run(["follow_into_dungeon", "healer_follows_ooc"], logout=False)
    assert [r["verdict"] for r in rep["scenarios"]] == [bg.PASS, bg.PASS]
    gms = _gm_texts(world)
    i = gms.index(".gm off")
    assert gms[i - 2:i + 1] == [".gm on", ".tele ulda", ".gm off"]
    assert ".gm on" in gms[i + 1:]  # scenario cleanup turns GM back on


def test_die_only_on_a_confirmed_selection(world_factory):
    world, gate, cfg = world_factory()
    gate.call("teleport", x=-236.4, y=60.4, z=-45.5, map=70)
    mob = gate.summon(4851)
    world.rpc_target = lambda p: {}  # the selection never sticks
    notes = gate.kill_leftovers()
    assert ".die" not in _gm_texts(world)
    assert any("could not select" in n for n in notes)
    assert not world.creatures[mob["guid"]]["dead"]


def test_gate_reports_inconclusive_when_turtle_agent_is_not_in_world(world_factory):
    world, gate, cfg = world_factory()
    world.leader["state"] = "logged_out"
    rep = gate.run(bg.ORDER)
    assert rep["verdict"] == bg.INCONCLUSIVE
    assert all(r["evidence"][0].startswith("not run: turtle-agent is 'logged_out'") for r in rep["scenarios"])
    assert rep["teardown"]["result"] == "not run"


def test_gate_budget_turns_a_slow_scenario_inconclusive(world_factory):
    def patch(raw):  # the 45 yd walk alone takes over 6 s, in legs of 15 yd
        raw["scenarios"]["healer_follows_ooc"]["budget_s"] = 2
    world, gate, cfg = world_factory(config_patch=patch)
    rep = gate.run(["healer_follows_ooc"], logout=False)
    r = rep["scenarios"][0]
    assert r["verdict"] == bg.INCONCLUSIVE and any("budget" in e for e in r["evidence"])


def test_cli_end_to_end_on_the_fake_servers(world_factory, tmp_path, monkeypatch, capsys):
    world, _, _ = world_factory()
    clock, srv = world.clock, world.servers
    cli = _cli()
    monkeypatch.setattr(cli.bg, "Gate", functools.partial(bg.Gate, clock=clock.now, sleep=clock.sleep, wall=clock.now))
    monkeypatch.setenv("BUILDGATE_DASHBOARD_USER", "u")
    monkeypatch.setenv("BUILDGATE_DASHBOARD_PASSWORD", "p")
    code = cli.main(["--config", CONFIG, "--rpc", srv.rpc_addr, "--dashboard-url", srv.dash_url,
                     "--dashboard-config", str(tmp_path / "none.json"), "--out-dir", str(tmp_path),
                     "--label", "fake", "--scenarios", "follow_into_dungeon,no_jumps_in_dungeon", "--quiet"])
    assert code == 0
    rep = json.loads((tmp_path / "buildgate-fake.json").read_text())
    assert [r["verdict"] for r in rep["scenarios"]] == [bg.PASS, bg.PASS]
    assert rep["teardown"]["result"] == "logged out"
    assert (tmp_path / "buildgate-fake.md").read_text().startswith("# Build gate fake")
    assert "PASS:" in capsys.readouterr().out


# ---------------------------------------------------------------- gather: summons refused in combat

def _party_in_dungeon(world_factory, bugs=()):
    world, gate, cfg = world_factory(bugs=bugs)
    gate.run(["follow_into_dungeon"], logout=False)
    names = gate.ensure_party().values()
    for b in world.bots.values():  # stranded 60 yd away, held there
        b.mode, b.x = "stay", b.x + 60
    return world, gate, list(names)


def test_gather_resends_the_summon_once_the_bots_are_out_of_combat(world_factory):
    world, gate, names = _party_in_dungeon(world_factory)
    for b in world.bots.values():
        b.combat_until = world.now() + 7
    gate.gather(names, wait_s=20)
    summons = [p["text"] for m, p in world.calls if m == "gm" and p["text"].startswith(".bot summon")]
    assert len(summons) == len(names)  # none sent while in combat, one each after


def test_gather_error_carries_the_servers_refusal_and_is_bounded(world_factory):
    world, gate, names = _party_in_dungeon(world_factory)
    for b in world.bots.values():
        b.combat_until = world.now() + 1000
    with pytest.raises(bg.ScenarioSetupError) as err:
        gate.gather(names, wait_s=15)
    assert "bots not with the player after summon" in str(err.value)
    # in combat: nothing was sent, so no reply to quote
    assert "summons sent: 0" in str(err.value)
    assert "no server reply seen" in str(err.value)


def test_gather_gives_up_after_three_summons_and_quotes_the_reply(world_factory):
    world, gate, names = _party_in_dungeon(world_factory)
    for b in world.bots.values():
        b.combat_until = world.now() + 1000
    gate.sample = lambda rec, nm, _s=gate.sample: _out_of_combat(_s(rec, nm))
    with pytest.raises(bg.ScenarioSetupError) as err:
        gate.gather(names, wait_s=40)
    sent = [p["text"] for m, p in world.calls if m == "gm" and p["text"].startswith(".bot summon")]
    assert len(sent) == 3 * len(names)
    assert "in combat and cannot be summoned" in str(err.value)


def _out_of_combat(s):
    for b in (s.get("bots") or {}).values():
        b["state"], b["strategy"] = "idle", "chat, follow, loot"
    return s


# ---------------------------------------------------------------- setup: the party is ready before a scenario starts

def _mob(world, dx=5.0, dy=3.0, in_combat=True, stuck=False, entry=4852):
    """A creature dx, dy yards from the leader; stuck: the party keeps fighting it until it dies."""
    L = world.leader
    g = world._spawn(entry, L["map"], L["x"] + dx, L["y"] + dy, L["z"])
    world.creatures[g]["in_combat"] = in_combat
    if stuck:
        world.stuck.add(g)
        for n in world.party:  # the fight is on now; the fake keeps the bots in combat from the next tick on
            world.bots[n].combat_until = world.now() + 1.0
    return g


def _call_index(world, method, pred):
    return next(i for i, (m, p) in enumerate(world.calls) if m == method and pred(p))


def test_wait_calm_finishes_a_fight_that_outlasts_the_wait_with_the_bots_attack(world_factory):
    world, gate, cfg = world_factory()
    names = list(gate.ensure_party().values())
    gate.gm_on()
    mob = _mob(world, stuck=True)
    t0 = world.now()
    gate.wait_calm(names, 90)
    assert world.now() - t0 >= 90  # the whole 90 s were waited before the fight was finished
    assert world.creatures[mob]["dead"]
    assert ".die" not in _gm_texts(world)  # the party's own attack finished it
    assert _call_index(world, "target", lambda p: p.get("guid") == mob) < \
        _call_index(world, "gm", lambda p: p["text"] == ".bot action attack")  # selection confirmed first
    assert any(s.startswith("wait: party still in combat") and "after 90.0 s; finishing the fight" in s
               for s in gate.setup_log), gate.setup_log
    assert any("killed by .bot action attack after" in s for s in gate.setup_log), gate.setup_log


def test_finish_fight_gm_dies_only_the_mob_the_party_fights_when_the_attack_is_refused(world_factory):
    world, gate, cfg = world_factory(bugs=["attack_refused"])
    names = list(gate.ensure_party().values())
    gate.gm_on()
    fighting = _mob(world, stuck=True)  # the party's attacker, 6 yd from a bot
    calm = _mob(world, dx=6.0, dy=-4.0, in_combat=False)  # next to it, not fighting
    far = _mob(world, dx=60.0, dy=0.0)  # in combat, but 60 yd from the party: not theirs
    gate.wait_calm(names, 30)
    assert world.creatures[fighting]["dead"]
    assert not world.creatures[calm]["dead"] and not world.creatures[far]["dead"]
    dies = [i for i, (m, p) in enumerate(world.calls) if m == "gm" and p["text"] == ".die"]
    assert len(dies) == 1
    assert _call_index(world, "target", lambda p: p.get("guid") == fighting) < dies[0]  # confirmed before the .die
    assert any("finish fight: GM .die on Stonevault Oracle" in s for s in gate.setup_log), gate.setup_log


def test_wait_calm_gives_up_naming_the_fight_when_no_creature_is_left_to_finish(world_factory):
    world, gate, cfg = world_factory()
    names = list(gate.ensure_party().values())
    for n in names:
        world.bots[n].combat_until = world.now() + 1000  # in combat, with nothing in combat near them
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="no creature in combat near the fighting bots"):
        gate.wait_calm(names, 30)
    assert world.now() - t0 >= 30  # the wait ran its course first
    assert not any(p["text"] in (".bot action attack", ".die") for m, p in world.calls if m == "gm")


def test_gather_sends_the_summon_again_when_the_first_one_is_lost(world_factory):
    world, gate, names = _party_in_dungeon(world_factory, bugs=["summon_lost_once"])
    world.summon_sent = {}
    gate.setup_log = []
    gate.gather(names, wait_s=20)
    assert all(world.summon_sent[n] == 2 for n in names)  # the first summon of each was lost, the second landed
    assert all(world.bots[n].map == 70 for n in names)
    assert any(s.startswith("wait: bots with the leader within 30 yd") and "summons sent: 8" in s
               for s in gate.setup_log), gate.setup_log


def test_gather_summons_bots_on_another_map_only_once_the_leader_is_in_place(world_factory):
    world, gate, cfg = world_factory(bugs=["slow_leader"])
    names = list(gate.ensure_party().values())
    for n in names:
        world.bots[n].mode = "stay"  # no automatic follow across maps: only the summon moves them
    gate.gm_on()
    gate.goto(cfg["dungeon"]["enter"])  # .tele ulda returns while the leader is still arriving
    assert world.path is not None
    world.summon_sent, world.summons = {}, []
    gate.setup_log = []
    gate.gather(names, wait_s=20)
    assert world.summons and not any(arriving for _, arriving in world.summons)  # none sent during the arrival
    assert all(world.summon_sent[n] == 1 for n in names)  # one summon each, none lost
    assert all(world.bots[n].map == 70 for n in names)
    assert any(s.startswith("wait: leader in place on map 70") for s in gate.setup_log), gate.setup_log


def test_a_leader_still_arriving_past_the_settle_limit_stops_the_summons(world_factory):
    world, gate, cfg = world_factory(bugs=["slow_leader"], config_patch=lambda c: c.update(settle_wait_s=2.0))
    names = list(gate.ensure_party().values())
    for n in names:
        world.bots[n].mode = "stay"
    gate.gm_on()
    gate.goto(cfg["dungeon"]["enter"])
    with pytest.raises(bg.ScenarioSetupError, match="leader not in place within 2 s"):
        gate.gather(names, wait_s=20)
    assert not any(m == "gm" and p["text"].startswith(".bot summon") for m, p in world.calls)  # nothing mid-arrival


def test_ready_waits_for_the_party_to_rest_and_logs_how_long(world_factory):
    world, gate, cfg = world_factory()
    names = list(gate.ensure_party().values())
    healer = world.bots[names[1]]  # the config's party order: tank, healer, mage, rogue
    healer.eat_until = world.now() + 12
    gate.setup_log = []
    gate.ready(names, 30, rest_wait_s=60)
    assert healer.eat_until <= world.now()
    line = next(s for s in gate.setup_log if s.startswith("wait: rested"))
    assert 10 <= float(line.rsplit(" after ", 1)[1].split()[0]) <= 14, line


def test_ready_names_the_bot_that_never_rests(world_factory):
    world, gate, cfg = world_factory()
    names = list(gate.ensure_party().values())
    world.bots[names[1]].eat_until = world.now() + 10_000  # eating for good with nothing gained: no progress
    with pytest.raises(bg.ScenarioSetupError, match="not rested .* before the scenario starts within 5 s: Niabrenans"):
        gate.ready(names, 30, rest_wait_s=5)


# live b18 (2026-10-13): the priest's buff -> drink -> buff loop kept her out of full mana for 68 s of the rest wait. The
# fixture is her real dashboard rows (botwatch, 2 s apart) over the wait; _b18_priest(t) holds the row seen at t.
_B18_ROWS = fixture("b18_belstelhael_rest.json")["rows"]


def _b18_priest(t, shift=0.0):
    row = [r for r in _B18_ROWS if r["t"] <= t + shift + 1e-6][-1]
    return {"name": "Belstelhael", "map": 70, "x": 0.0, "y": 0.0, "strategy": "chat, follow, loot",
            **{k: row[k] for k in ("hp", "max_hp", "power", "max_power", "power_type", "state", "last_action")}}


def _stalled_priest(t):
    """Belstelhael stuck at 2626 of 3006 mana: not drinking, nothing gained."""
    return {"name": "Belstelhael", "map": 70, "x": 0.0, "y": 0.0, "state": "idle", "last_action": "check values",
            "strategy": "chat, follow, loot", "hp": 1552, "max_hp": 1552, "power": 2626, "max_power": 3006,
            "power_type": "mana"}


def _drinking_priest(t):
    """Belstelhael drinking for good with no mana gained: eating or drinking alone is not progress."""
    return dict(_stalled_priest(t), state="resting", last_action="drink")


def _oscillating_priest(t):
    """Belstelhael's mana swings 2626 <-> 2540 (a buff's cost, then the regen tick back), she never drinks and nothing
    ever reaches a new high."""
    s = int(t + 1e-6)
    return dict(_stalled_priest(t), power=2540 if (s // 4) % 2 else 2626, last_action="buff")


def _drink_buff_priest(t):
    """Belstelhael drinks 4 s (+100 mana a second), then a buff costs her what she drank: every cycle is the same, the
    mana is never above its first peak, and she never gets full."""
    s = int(t + 1e-6) % 8
    drinking = s < 4
    return dict(_stalled_priest(t), power=2400 + 100 * s if drinking else 2400,
                state="resting" if drinking else "idle", last_action="drink" if drinking else "buff")


def _gaining_priest(t):
    """Belstelhael gaining 1 mana every 2 s and never full: a bot that keeps making progress."""
    return dict(_stalled_priest(t), power=min(3005, 2626 + int(t + 1e-6) // 2))


def _regen_priest(t):
    """Belstelhael regaining HP at 1 a second without drinking (mana full): full HP at 90 s."""
    return {"name": "Belstelhael", "map": 70, "x": 0.0, "y": 0.0, "state": "idle", "last_action": "check values",
            "strategy": "chat, follow, loot", "hp": min(1552, 1462 + int(t + 1e-6)), "max_hp": 1552,
            "power": 3006, "max_power": 3006, "power_type": "mana"}


def _rest_on(world_factory, trace, down=lambda t: False):
    """A gate whose dashboard samples are trace(seconds since now) on the fake world's clock; down(seconds) true: that
    sample is a failed read (none of the party in it)."""
    world, gate, cfg = world_factory()
    t0 = world.now()

    def sample(rec, names):
        t = world.now() - t0
        return {"t": round(t, 2), "leader": None, "bots": {} if down(t) else {"belstelhael": trace(t)}}

    gate.sample = sample
    return world, gate


@pytest.mark.parametrize("start", [0, 1, 2, 3])  # the real wait began somewhere in the first seconds of these rows
def test_rest_wait_runs_past_60_s_on_the_real_b18_trace(world_factory, start):
    # her mana oscillates (buff, drink, buff) and sits below its earlier high of 2532 for 34 s of the wait; the 10 s
    # before the 60 s mark show it rising while she drinks (2034 -> 2282 -> 2410): progress, so the wait goes on until
    # she is full at 68 s
    world, gate = _rest_on(world_factory, lambda t: _b18_priest(t, start))
    t0 = world.now()
    gate.require_rested(["Belstelhael"], 60, "before the scenario starts")  # the wait goes on past 60 s: no error
    assert 67 - start <= world.now() - t0 <= 69 - start
    line = next(s for s in gate.setup_log if s.startswith("wait: rested"))
    assert "past the 60 s wait while a bot made progress" in line, line


def test_rest_wait_runs_past_60_s_while_hp_climbs_without_drinking(world_factory):
    world, gate = _rest_on(world_factory, _regen_priest)
    t0 = world.now()
    gate.require_rested(["Belstelhael"], 60, "before the scenario starts")
    assert 89 <= world.now() - t0 <= 91


def test_rest_wait_still_ends_at_its_wait_when_no_bot_makes_progress(world_factory):
    world, gate = _rest_on(world_factory, _stalled_priest)
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="not rested .* before the scenario starts within 60 s: Belstelhael"):
        gate.require_rested(["Belstelhael"], 60, "before the scenario starts")
    assert 59 <= world.now() - t0 <= 61
    line = next(s for s in gate.setup_log if s.startswith("wait: still not rested"))
    assert "the 60 s wait ran out with no bot making progress" in line, line


def test_rest_wait_stops_at_the_hard_cap_while_a_bot_keeps_gaining(world_factory):
    world, gate = _rest_on(world_factory, _gaining_priest)
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="not rested .* before the scenario starts within the 150 s cap: Belstelhael"):
        gate.require_rested(["Belstelhael"], 60, "before the scenario starts")
    assert 149 <= world.now() - t0 <= 151


@pytest.mark.parametrize("trace", [_oscillating_priest, _drinking_priest])
def test_rest_wait_makes_no_progress_from_oscillation_or_from_eating_alone(world_factory, trace):
    world, gate = _rest_on(world_factory, trace)
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="not rested .* before the scenario starts within 60 s: Belstelhael"):
        gate.require_rested(["Belstelhael"], 60, "before the scenario starts")
    assert 59 <= world.now() - t0 <= 61


def test_rest_wait_of_a_bot_that_drinks_and_buffs_for_nothing_runs_to_the_cap_by_the_rule(world_factory):
    # accepted by the rule: every drink raises her mana, so each sample while she drinks is progress, however flat the
    # cycle is; the 150 s cap is what ends it (she was full at 68 s in the real trace)
    world, gate = _rest_on(world_factory, _drink_buff_priest)
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="not rested .* before the scenario starts within the 150 s cap: Belstelhael"):
        gate.require_rested(["Belstelhael"], 60, "before the scenario starts")
    assert 149 <= world.now() - t0 <= 151


def test_one_failed_dashboard_sample_past_the_wait_does_not_end_a_progressing_rest(world_factory):
    world, gate = _rest_on(world_factory, _gaining_priest, down=lambda t: 61 <= t < 62)  # one failed read past 60 s
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="within the 150 s cap: Belstelhael"):  # the cap, not "no data"
        gate.require_rested(["Belstelhael"], 60, "before the scenario starts")
    assert 149 <= world.now() - t0 <= 151


def test_three_failed_dashboard_samples_in_a_row_past_the_wait_end_a_progressing_rest(world_factory):
    world, gate = _rest_on(world_factory, _gaining_priest, down=lambda t: 61 <= t < 64)
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="the dashboard gave no bot data"):
        gate.require_rested(["Belstelhael"], 60, "before the scenario starts")
    assert 62 <= world.now() - t0 <= 64


def test_failed_samples_that_are_not_in_a_row_do_not_add_up(world_factory):
    world, gate = _rest_on(world_factory, _gaining_priest, down=lambda t: int(t + 1e-6) in (61, 62, 64, 65, 67))
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="within the 150 s cap: Belstelhael"):
        gate.require_rested(["Belstelhael"], 60, "before the scenario starts")
    assert 149 <= world.now() - t0 <= 151


def test_rest_wait_ends_at_the_time_budget_before_the_hard_cap_and_names_the_bot(world_factory):
    world, gate = _rest_on(world_factory, _gaining_priest)  # progressing: only the time budget can end it
    gate.deadline = world.now() + 100  # the time cap is 100 s away: the wait stops REST_MARGIN_S short of it
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError) as err:
        gate.require_rested(["Belstelhael"], 60, "before the scenario starts")
    assert not isinstance(err.value, bg.BudgetExceeded), err.value
    assert "not rested" in str(err.value) and "Belstelhael" in str(err.value), err.value
    assert 94 <= world.now() - t0 <= 96


def test_moon_recheck_with_extend_false_ends_at_its_15_s(world_factory):
    world, gate = _rest_on(world_factory, lambda t: _b18_priest(t, 2))  # she is progressing, but this wait does not extend
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="not rested .* right before the pull within 15 s: Belstelhael"):
        gate.require_rested(["Belstelhael"], 15, "right before the pull", extend=False)
    assert 14 <= world.now() - t0 <= 16


def test_a_failed_dashboard_sample_never_counts_as_rested(world_factory):
    world, gate, cfg = world_factory()

    def down(names):
        raise DashboardUnavailable("dashboard unreachable: test")

    gate.d.bots = down  # every sample is then a failed read that lists nobody
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="not rested .* within 10 s: Belstelhael"):
        gate.require_rested(["Belstelhael"], 10, "before the scenario starts")
    assert 9 <= world.now() - t0 <= 11


def test_a_figure_missing_from_one_sample_is_skipped_not_read_as_zero(world_factory):
    def trace(t):
        bot = dict(_stalled_priest(t), hp=1500)  # steady, not full: busy, and nothing gained
        if int(t + 1e-6) == 58:
            del bot["hp"]  # read as 0, the 1500 at 59 s would be a new high and carry the wait past 60 s
        return bot

    world, gate = _rest_on(world_factory, trace)
    t0 = world.now()
    with pytest.raises(bg.ScenarioSetupError, match="within 60 s: Belstelhael"):
        gate.require_rested(["Belstelhael"], 60, "before the scenario starts")
    assert 59 <= world.now() - t0 <= 61


def test_scenario_evidence_says_why_it_started_late(world_factory):
    world, gate, cfg = world_factory()
    m, x, y, z = TELE["badlands"]  # the follow scenario's start spot: a fight the party walks into
    mob = world._spawn(4852, m, x + 5, y + 3, z)
    world.creatures[mob]["in_combat"] = True
    world.stuck.add(mob)
    r = gate.run(["follow_into_dungeon"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r["evidence"]
    assert any(e.startswith("wait: party still in combat") and "after 90.0 s" in e for e in r["evidence"]), r["evidence"]
    assert any("killed by .bot action attack" in e for e in r["evidence"]), r["evidence"]
    assert world.creatures[mob]["dead"]


def test_a_setup_wait_stops_at_the_setup_cap(world_factory):
    world, gate, cfg = world_factory(config_patch=lambda c: c.update(setup_budget_s=30))
    m, x, y, z = TELE["badlands"]
    mob = world._spawn(4852, m, x + 5, y + 3, z)
    world.creatures[mob]["in_combat"] = True
    world.stuck.add(mob)
    r = gate.run(["follow_into_dungeon"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.INCONCLUSIVE and any("setup time cap (30 s) used up" in e for e in r["evidence"]), \
        r["evidence"]
    assert r["seconds"] <= 35 and r["setup_s"] is None
    assert not world.creatures[mob]["dead"]  # the cap ended the wait before the fight was touched


def test_a_long_setup_does_not_eat_the_scenarios_budget(world_factory):
    # setup: a 90 s calm wait and the fight finished, well past the scenario's 30 s budget; the budget starts after
    world, gate, cfg = world_factory(config_patch=lambda c: c["scenarios"]["follow_into_dungeon"].update(budget_s=30))
    m, x, y, z = TELE["badlands"]
    mob = world._spawn(4852, m, x + 5, y + 3, z)
    world.creatures[mob]["in_combat"] = True
    world.stuck.add(mob)
    r = gate.run(["follow_into_dungeon"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r["evidence"]
    assert r["setup_s"] > 90 and r["seconds"] - r["setup_s"] <= 30
    assert any(e.startswith("setup ready after") for e in r["evidence"]), r["evidence"]
    assert world.creatures[mob]["dead"]


def test_cli_dry_run_states_the_setup_before_each_scenario(capsys):
    assert _cli().main(["--config", CONFIG, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "setup before each scenario: out of combat (up to 90 s;" in out
    assert "rested (full HP and mana)" in out


# ---------------------------------------------------------------- safety: the fight finisher never picks a non-mob

def _crowd(world, at=None, m=None):
    """Units in combat right next to the party that are never the gate's to finish: a hunter pet (0xF140, even one
    flagged hostile), a friendly vendor NPC (NPC flags) whose target is a bot, another player whose target is a bot,
    and a hostile creature fighting that stranger. Returns their guids; the party's own player rows come on top."""
    L = world.leader
    x, y, z = at if at else (L["x"], L["y"], L["z"])
    bot = next(iter(world.bots.values())).guid if world.bots else None
    stranger = world.add_other("player", "Strangerx", x + 4, y - 2, z, target=bot, m=m)
    return {
        world.add_other("pet", "Wolf", x + 1.5, y + 1, z, target=bot, m=m),
        world.add_other("pet", "Imp", x + 1.8, y - 1, z, target=bot, m=m, hostile=True),
        world.add_other("npc", "Vendor Hal", x + 2, y + 2, z, target=bot, entry=3000, m=m),
        stranger,
        world.add_other("npc", "Ambusher", x + 3, y - 3, z, target=stranger, entry=4853, m=m, hostile=True),
    }


def _never_touched(world, forbidden):
    party = {b.guid for b in world.bots.values()} | {world.leader["guid"]}
    bad = (forbidden | party)
    selected = [p.get("guid") for m, p in world.calls if m == "target"]
    assert not bad & set(selected), [g for g in selected if g in bad]
    assert not bad & set(world.attack_orders), world.attack_orders
    assert not bad & set(world.died), world.died
    assert not any(world.others[g]["dead"] for g in forbidden)
    assert all(b.hp > 0 for b in world.bots.values())


@pytest.mark.parametrize("bugs", [(), ("attack_refused",)])
def test_finish_fight_picks_only_the_mob_fighting_the_party_never_a_player_pet_or_npc(world_factory, bugs):
    world, gate, cfg = world_factory(bugs=bugs)
    names = list(gate.ensure_party().values())
    gate.gm_on()
    forbidden = _crowd(world)
    mob = _mob(world, dx=8.0, dy=5.0, stuck=True)  # further than every party row, pet, NPC and player
    gate.wait_calm(names, 30)
    assert world.creatures[mob]["dead"]
    assert world.attack_orders == [mob]
    assert world.died == ([mob] if bugs else [])
    _never_touched(world, forbidden)


def test_finish_fight_with_only_players_pets_and_npcs_in_combat_touches_nothing(world_factory):
    world, gate, cfg = world_factory()
    names = list(gate.ensure_party().values())
    gate.gm_on()
    for n in names:  # the party rows are in combat, 0 yd from the bots: the old rule took one as "the mob"
        world.bots[n].combat_until = world.now() + 1000
    forbidden = _crowd(world)
    with pytest.raises(bg.ScenarioSetupError, match="no creature in combat near the fighting bots"):
        gate.wait_calm(names, 20)
    assert not any(p["text"] in (".bot action attack", ".die") for m, p in world.calls if m == "gm")
    _never_touched(world, forbidden)


@pytest.mark.parametrize("bugs", [(), ("attack_refused",)])
def test_scenario_setup_finishes_only_the_mob_in_a_crowd(world_factory, bugs):
    world, gate, cfg = world_factory(bugs=bugs)
    m, x, y, z = TELE["badlands"]  # the follow scenario's start: a fight in a crowd the party walks into
    forbidden = _crowd(world, at=(x, y, z), m=m)
    mob = world._spawn(4852, m, x + 6, y + 4, z)
    world.creatures[mob]["in_combat"] = True
    world.stuck.add(mob)
    r = gate.run(["follow_into_dungeon"], logout=False)["scenarios"][0]
    assert r["verdict"] == bg.PASS, r["evidence"]
    assert world.creatures[mob]["dead"] and world.attack_orders == [mob]
    _never_touched(world, forbidden)


def test_whole_gate_in_a_crowd_never_touches_a_player_pet_or_npc(world_factory):
    world, gate, cfg = world_factory()
    m, x, y, z = TELE["badlands"]
    forbidden = _crowd(world, at=(x, y, z), m=m)
    for sp in ("spot", "pull_at"):
        p = cfg["scenarios"]["ranged_reach_in_dungeon"][sp]
        forbidden |= _crowd(world, at=(p["x"] + 3, p["y"] + 3, p["z"]), m=70)
    rep = gate.run(bg.ORDER)
    assert {r["name"]: r["verdict"] for r in rep["scenarios"]} == {n: bg.PASS for n in bg.ORDER}, \
        json.dumps(rep["scenarios"], indent=1)
    _never_touched(world, forbidden)


def _refused_fight(world_factory):
    world, gate, cfg = world_factory(bugs=["attack_refused"])
    names = list(gate.ensure_party().values())
    gate.gm_on()
    mob = _mob(world, stuck=True)
    return world, gate, names, mob


def test_gm_die_refused_when_the_selection_changes_before_it_is_sent(world_factory):
    world, gate, names, mob = _refused_fight(world_factory)
    tank = world.bots[names[0]].guid
    status = world.rpc_status
    def drifting(p):  # once the order is out, something else retargets the player between checks
        out = status(p)
        if world.attack_orders and out["target"] == mob and sum(1 for c, _ in world.calls if c == "units") % 2:
            world.leader["target"] = tank
            out = status(p)
        return out
    world.rpc_status = drifting
    with pytest.raises(bg.ScenarioSetupError, match="refused"):
        gate.wait_calm(names, 10)
    assert world.died == [] and ".die" not in _gm_texts(world)
    assert any(".die on" in s and "refused" in s for s in gate.setup_log), gate.setup_log


def test_gm_die_refused_when_the_mob_stops_fighting_the_party(world_factory):
    world, gate, names, mob = _refused_fight(world_factory)
    stranger = world.add_other("player", "Strangerx", world.leader["x"] + 30, world.leader["y"], in_combat=True)
    gm = world.rpc_gm
    def turn(p):  # the order is refused, and the mob turns on a stranger
        out = gm(p)
        if p["text"] == ".bot action attack":
            world.stuck.discard(mob)
            world.creatures[mob]["target"] = stranger
        return out
    world.rpc_gm = turn
    with pytest.raises(bg.ScenarioSetupError, match="not fighting the party"):
        gate.wait_calm(names, 10)
    assert world.died == [] and not world.creatures[mob]["dead"]
    assert any("refused: not fighting the party" in s for s in gate.setup_log), gate.setup_log


def test_gm_die_never_sent_with_gm_off(world_factory):
    world, gate, names, mob = _refused_fight(world_factory)
    gate.gm = False
    with pytest.raises(bg.ScenarioSetupError, match="GM is off"):
        gate.wait_calm(names, 10)
    assert world.died == [] and world.attack_orders == [mob]
    gate.gm = None  # unknown counts as off too
    with pytest.raises(bg.ScenarioSetupError, match="GM is off"):
        gate.finish_fight(names)
    assert world.died == []


def test_mob_ineligible_names_every_rule():
    party = {0x20, 0x10}
    mob = {"guid": "0xF130000012D40100", "type": "creature", "hostile": True, "in_combat": True, "target": "0x20"}
    assert bg.mob_ineligible(mob, party) == ""
    assert bg.mob_ineligible({**mob, "target": "0x0000000000000010"}, party) == ""  # fighting the leader
    for patch, why in [({"guid": "0x0000000000000020"}, "a party member"), ({"in_party": True}, "a party member"),
                       ({"type": "player", "guid": "0x0000000000000999"}, "not a creature"),
                       ({"guid": "0xF140000000000105"}, "not a creature"), ({"hostile": False}, "not hostile"),
                       ({"hostile": None}, "not hostile"), ({"dead": True}, "dead"), ({"in_combat": False}, "not in combat"),
                       ({"target": "0x0000000000000999"}, "not fighting the party"), ({"target": None}, "not fighting"),
                       ({"guid": None}, "no guid")]:
        assert why in bg.mob_ineligible({**mob, **patch}, party), patch
    assert bg.mob_ineligible(None, party) == "not in view"


def test_kill_leftovers_never_dies_a_party_member_or_a_player(world_factory):
    world, gate, cfg = world_factory()
    names = list(gate.ensure_party().values())
    gate.gm_on()
    b = world.bots[names[0]]
    gate.summons.append({"guid": b.guid, "entry": 0, "name": b.name})  # a bad summon record
    notes = gate.kill_leftovers()
    assert world.died == [] and any("not a creature outside the party" in n for n in notes)

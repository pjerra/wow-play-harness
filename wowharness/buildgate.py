"""Build gate: headless regression scenarios run after each TortoiseBots deploy, in place of most of
the manual in-game checks done with the real-client harness.

The pieces:
- evaluate_* functions are pure: a recording (dashboard samples, combat records) in, an Outcome
  (pass / fail / inconclusive plus evidence lines) out. The unit tests drive them with recorded and
  fake streams.
- Gate drives turtle-agent (HeadlessClient JSON-RPC) and the bot dashboard, runs the scenarios and owns
  the safety rules, so no scenario repeats them:
    * every `.gm off` goes through Gate.gm_off, which teleports to the safe spot with GM on first;
    * before logout: stop_attack, leave_group, `.gm on`; a refused logout gets `.gm on`, a wait and one
      retry, then is reported;
    * fights use the dungeon's own packs, brought back with `.respawn` when dead (a creature `.npc summon`ed
      with GM on gets the GM's friendly faction 35, and summoning with GM off would aggro the player);
      creatures the gate summoned and that are still alive are killed (`.die` only on a verified target);
    * a scenario starts only when the party is ready (Gate.ready): out of combat (a fight still on after
      calm_wait_s is finished: `.bot action attack` on the mob the party fights, else a GM `.die` on that same
      confirmed target), every bot on the leader's map within range (summons re-sent, 3 at most) and rested.
      Each setup wait is logged and kept in the scenario's evidence, so a late start says why. Setup has its own
      cap (setup_budget_s); the scenario's budget_s starts once the party is ready.
    * the fight finisher only ever targets a mob fighting the party (mob_ineligible): a hostile creature (guid high
      0xF130), not in the party, not a pet (0xF140), player or NPC with flags, whose target is the leader or a party
      member. The GM `.die` re-checks all of that by guid on the confirmed selection right before it is sent, needs
      GM on and the selection unchanged, and refuses (logged) otherwise.
- Spots, creature entries and thresholds come from a scenario config file (tools/buildgate_uldaman.json).
  The harness never reads turtle-agent's settings (account credentials stay there); the dashboard login
  comes from the environment or the local harness settings file, never from the scenario config.

Thresholds and setups are tonight's manual checks (TortoiseBots-audit/ingame/): build11-gameserver.md,
b11-focus.md, ulda-b11-walk.md, repro-129-follow.md, build13-gameserver.md, repro-124-vm*.md.

Time: Gate takes clock/sleep/wall callables, so tests can run the whole gate on a virtual clock.
"""
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .combat import parse_lines
from .dashboard import DashboardUnavailable
from .headless import HeadlessError

PASS, FAIL, INCONCLUSIVE = "pass", "fail", "inconclusive"
REACH_HEAL = "reach party member to heal"
REACH_SPELL = "reach spell"
# Polymorph ranks 1-4 (sheep) and the Turtle/Pig variants.
POLYMORPH_IDS = frozenset({118, 12824, 12825, 12826, 28271, 28272})
COMBAT_KINDS = ["melee", "spell_damage", "periodic", "spell_start", "spell_go", "aura_applied", "death", "kill", "attack_start"]
CHAT_KINDS = ["chat", "whisper", "system"]

ORDER = ["follow_into_dungeon", "healer_follows_ooc", "stay_then_order_follows",
         "ranged_reach_in_dungeon", "moon_sheep_lands", "no_jumps_in_dungeon"]

# Per-scenario defaults; the config file's "scenarios" section overrides them key by key.
DEFAULTS = {
    "follow_into_dungeon": {"within_s": 20, "extra_s": 30, "rest_wait_s": 60, "budget_s": 180},
    "stay_then_order_follows": {"bot": "rogue", "mob_entry": 4852, "mob_hp": 800, "fight_max_s": 60,
                                "settle_s": 20, "follow_max_yd": 10, "min_move_yd": 10, "budget_s": 180},
    "healer_follows_ooc": {"healer": "healer", "end_max_yd": 15, "stall_s": 20, "settle_s": 30,
                           "ooc_wait_s": 45, "budget_s": 150},
    "ranged_reach_in_dungeon": {"casters": ["healer", "mage"], "pull_entries": [4851, 4852],
                                "order": ".bot attack {tank}", "standstill_s": 10, "fight_max_s": 90,
                                "quiet_s": 6, "budget_s": 180},
    "moon_sheep_lands": {"mage": "mage", "pack_entries": [4851, 4852], "moon_index": 0, "pull_index": 1,
                         "order": ".bot attack {tank}", "within_s": 10, "watch_s": 45, "quiet_s": 5,
                         "rest_wait_s": 45, "rest_recheck_s": 15, "budget_s": 180},
    "no_jumps_in_dungeon": {"min_samples": 10, "budget_s": 90},
}
GATE_DEFAULTS = {"budget_s": 1200, "sample_s": 1.0, "leg_yd": 15.0, "gather_yd": 30.0,
                 "logout_retry_wait_s": 10.0, "teleport_tolerance_yd": 6.0,
                 # scenario setup (Gate.ready): out of combat first; a fight still on after calm_wait_s is finished
                 "calm_wait_s": 90.0, "fight_yd": 30.0, "attack_wait_s": 20.0, "settle_wait_s": 15.0,
                 # the cap on a scenario's setup (party hired, placed, ready); its budget_s starts once the party is ready
                 "setup_budget_s": 180.0}
FIGHT_MAX_MOBS = 4  # the mobs one setup wait finishes at most
FIGHT_CALM_S = 15.0  # after a mob dies: how long the party may take to leave combat (and the dashboard to show it)
REST_WAIT_S = 60  # the party's rest before a scenario, for a scenario without rest_wait_s
REST_PROGRESS_S = 10.0  # a busy bot makes progress when its HP or power gained (a new high, or a rise while eating) within this many seconds
REST_NO_DATA_SAMPLES = 3  # failed dashboard samples in a row, past the rest wait, that end it
REST_HARD_CAP_S = 150.0  # a rest wait that keeps making progress ends here at the latest, counted from its start
REST_MARGIN_S = 5.0  # a rest wait ends this long before the time budget (setup cap or scenario budget) runs out
PACK_CONFIRM_S = 2.0  # a pack found alive is looked at again this much later: a creature killed a moment ago can still be listed alive
PACK_RESPAWN_WAIT_S = 12.0  # after `.respawn`, the pack is polled this long until every unit is listed alive (and out of combat)
MARK_WAIT_S = 4.0  # after a cc order: how long the raid_mark event for its icon is waited for (live: 0.13 s to 1.8 s)
STAY_REPLY_WAIT_S = 8.0  # the bot's reply to `stay` is read until it shows in the event log, this long after the whisper
ACK_REPLY_WAIT_S = 8.0  # the same for the TBM:ACTION_ACK / _ERR line of an order (live: the cc ACK came after a one-shot read)
SECRET_KEYS = re.compile(r"pass(word)?|secret|token|account|user(name)?|credential", re.I)


@dataclass
class Outcome:
    verdict: str
    evidence: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


class ScenarioSetupError(Exception):
    """The scenario could not set up its situation: inconclusive, not a bot failure."""


class BudgetExceeded(ScenarioSetupError):
    pass


# ---------------------------------------------------------------- sample helpers
# A sample: {"t": seconds since the recording started, "leader": {"map","x","y","z",...} or None,
#            "bots": {lowercase name: dashboard bot dict}}

def bot_of(sample: dict, name: str) -> dict | None:
    return (sample.get("bots") or {}).get(name.lower())


def strategies(bot: dict | None) -> set[str]:
    """The dashboard's strategy field ("chat, follow, follow jump, ...") as a set of names."""
    return {s.strip().lower() for s in str((bot or {}).get("strategy") or "").split(",") if s.strip()}


def dist2d(a: dict, b: dict) -> float:
    return math.hypot(float(a.get("x") or 0) - float(b.get("x") or 0), float(a.get("y") or 0) - float(b.get("y") or 0))


def leader_dist(sample: dict, name: str) -> float | None:
    b, lead = bot_of(sample, name), sample.get("leader")
    if not b or not lead or b.get("map") != lead.get("map"):
        return None
    return dist2d(b, lead)


def in_combat(bot: dict | None) -> bool:
    return bool(bot) and (bot.get("state") or "") == "combat"


HIGH_CREATURE = 0xF130  # turtle-agent guids: high 16 bits 0xF130 a creature, 0xF140 a pet or guardian, 0 a player


def guid_int(g) -> int | None:
    """A turtle-agent guid ("0xF130...", hex) as an int; None when absent or not hex."""
    try:
        v = int(str(g).strip().lower().removeprefix("0x"), 16) if g not in (None, "") else 0
    except ValueError:
        return None
    return v or None


def mob_ineligible(u: dict | None, party_guids: set[int]) -> str:
    """Why a `units` row may not be finished by the gate (a `.bot action attack` order or a GM `.die`), or "" when it
    may: it must be a living creature (guid high 0xF130, type creature: never a player, a party bot or a pet 0xF140),
    `hostile` (no NPC flags, not player-controlled: never a vendor, guard or quest NPC), not in the party, in combat,
    and fighting the party (its `target` is the leader or a party member)."""
    if not u:
        return "not in view"
    g = guid_int(u.get("guid"))
    if g is None:
        return "no guid"
    if g in party_guids or u.get("in_party"):
        return "a party member"
    if u.get("type") != "creature" or g >> 48 != HIGH_CREATURE:
        return f"not a creature (type {u.get('type')!r}, guid high 0x{g >> 48:04X})"
    if u.get("hostile") is not True:
        return "not hostile (NPC flags or player-controlled)"
    if u.get("dead"):
        return "dead"
    if not u.get("in_combat"):
        return "not in combat"
    if guid_int(u.get("target")) not in party_guids:
        return f"not fighting the party (target {u.get('target') or 'none'})"
    return ""


def _action(bot: dict | None) -> str:
    return str((bot or {}).get("last_action") or "").lower()


def _xy(d: dict) -> str:
    return f"({float(d.get('x') or 0):.1f},{float(d.get('y') or 0):.1f})"


def standstill_runs(samples: list[dict], name: str, actions, still_yd: float = 1.0, casts=None) -> list[dict]:
    """Stretches where the bot's last_action held one of `actions` (substring, any case) and the bot stayed
    within still_yd of where the stretch began. A sample without the bot (dashboard hiccup) does not break
    a stretch. Each run: start, end, seconds, action, x, y, map. `casts` (the bot's cast times on the samples'
    clock, see cast_times) cut a run: each cast-free stretch is a run of its own, so a caster that keeps casting
    from where it stands has no long run."""
    acts = [a.lower() for a in actions]
    runs, cur = [], None
    for s in samples:
        b = bot_of(s, name)
        if b is None:
            continue
        hit = next((a for a in acts if a in _action(b)), None)
        if hit and cur and b.get("map") == cur["map"] and dist2d(b, cur) <= still_yd:
            cur["end"] = s["t"]
            continue
        if cur:
            runs.append(cur)
            cur = None
        if hit:
            cur = {"start": s["t"], "end": s["t"], "action": hit, "x": b.get("x"), "y": b.get("y"), "map": b.get("map")}
    if cur:
        runs.append(cur)
    out = []
    for r in runs:
        out += _cut_at_casts(r, casts)
    return out


def _cut_at_casts(run: dict, casts) -> list[dict]:
    """The run itself, or its cast-free stretches when casts fall strictly inside it (a zero-length stretch, from
    two casts at one time, is dropped; a cast at the very start or end of a run cuts nothing)."""
    cuts = sorted(c for c in (casts or ()) if run["start"] < c < run["end"])
    if not cuts:
        return [{**run, "seconds": round(run["end"] - run["start"], 1)}]
    bounds = [run["start"], *cuts, run["end"]]
    return [{**run, "start": a, "end": b, "seconds": round(b - a, 1)} for a, b in zip(bounds, bounds[1:]) if b > a]


def cast_times(records: list[dict], name: str) -> list[float]:
    """The times (samples' clock) of the casts `name` made: records flagged cast_ok (spell_go, spell damage and,
    from the timeline's agent records, direct heals). Melee and DoT/HoT ticks are not casts: a tick is the effect
    of an earlier cast, so it does not break a standstill."""
    who = name.lower()
    return sorted(r["t"] for r in records if r.get("cast_ok") and (r.get("source") or "").lower() == who)


def _longest(runs: list[dict]) -> dict | None:
    return max(runs, key=lambda r: r["seconds"]) if runs else None


# ---------------------------------------------------------------- evaluations (pure)

def evaluate_follow_into_dungeon(samples: list[dict], bots: list[str], t_tele: float, map_id: int = 70,
                                 within_s: float = 20) -> Outcome:
    """All bots on map_id within within_s of the teleport, and each one's last out-of-combat strategy list
    after arriving holds `follow` (combat lists never hold it). Build 11: only 1 of 4 came in."""
    ev, fails, unknown, arrive = [], [], [], {}
    last_t = samples[-1]["t"] if samples else t_tele
    for n in bots:
        arr = next((s["t"] for s in samples if s["t"] >= t_tele and (b := bot_of(s, n)) and b.get("map") == map_id), None)
        if arr is None or arr - t_tele > within_s:
            seen = [b for s in samples if (b := bot_of(s, n))]
            where = (f"map {seen[-1].get('map')} {_xy(seen[-1])}, {seen[-1].get('state')} {seen[-1].get('last_action')!r}"
                     if seen else "never on the dashboard")
            if arr is None and last_t - t_tele < within_s:
                unknown.append(f"{n}: not on map {map_id} yet, only {last_t - t_tele:.1f} s watched (last {where})")
            else:
                at_tele = next((b for s in reversed(samples) if s["t"] <= t_tele and (b := bot_of(s, n))), None)
                doing = f"; at the teleport: {at_tele.get('state')} {at_tele.get('last_action')!r}" if at_tele else ""
                fails.append(f"{n}: not on map {map_id} within {within_s:g} s of the teleport"
                             + (f" (arrived at +{arr - t_tele:.1f} s)" if arr is not None else f" (last {where})") + doing)
            continue
        arrive[n] = round(arr - t_tele, 1)
        ev.append(f"{n}: on map {map_id} at +{arr - t_tele:.1f} s")
        ooc = [b for s in samples if s["t"] >= arr and (b := bot_of(s, n)) and b.get("map") == map_id and not in_combat(b)]
        if not ooc:
            unknown.append(f"{n}: only combat samples after arriving, strategy list not checked")
        elif "follow" in strategies(ooc[-1]):
            ev.append(f"{n}: strategies hold follow")
        else:
            fails.append(f"{n}: no `follow` in the strategy list after arriving: {ooc[-1].get('strategy')!r}")
    verdict = FAIL if fails else INCONCLUSIVE if unknown else PASS
    return Outcome(verdict, fails + unknown + ev, {"arrival_s": arrive, "watched_s": round(last_t - t_tele, 1)})


def evaluate_stay_then_follow(samples: list[dict], bot: str, t_order: float, t_walk_start: float,
                              t_walk_end: float, follow_max_yd: float = 10, min_move_yd: float = 10,
                              fought: bool | None = None) -> Outcome:
    """After `stay` and a `.bot attack <bot>` fight, the bot's out-of-combat strategy list holds `follow`
    again and it follows a walk (ends within follow_max_yd, having moved). Build 13: stay persisted."""
    ev, fails = [], []
    after = [s for s in samples if s["t"] >= t_order]
    if fought is None:
        fought = any(in_combat(bot_of(s, bot)) for s in after)
    if not fought:
        return Outcome(INCONCLUSIVE, [f"{bot} never showed combat after the attack order: no fight to test"])
    last_combat = max((s["t"] for s in after if in_combat(bot_of(s, bot))), default=t_order)
    if last_combat >= t_walk_start:
        return Outcome(INCONCLUSIVE, [f"{bot} was still fighting at t {last_combat:g} s, after the walk began at"
                                      f" {t_walk_start:g} s: the fight did not end in time"])
    ooc = [b for s in after if s["t"] > last_combat and (b := bot_of(s, bot)) and not in_combat(b)]
    follow_after = None
    if ooc:
        follow_after = "follow" in strategies(ooc[-1])
        line = f"{bot} strategies after the fight: {ooc[-1].get('strategy')!r}"
        (ev if follow_after else fails).append(line if follow_after else line + " - no `follow`")
    start = [s for s in samples if s["t"] <= t_walk_start and bot_of(s, bot)]
    end = [s for s in samples if s["t"] >= t_walk_end and leader_dist(s, bot) is not None]
    if not start or not end:
        return Outcome(FAIL if fails else INCONCLUSIVE, fails + ev + ["no samples around the walk"])
    b0, b1 = bot_of(start[-1], bot), bot_of(end[-1], bot)
    moved, final = dist2d(b0, b1), leader_dist(end[-1], bot)
    walk_line = f"{bot} moved {moved:.1f} yd during/after the walk, ended {final:.1f} yd from the leader"
    if final > follow_max_yd or moved < min_move_yd:
        fails.append(walk_line + f" (needs <= {follow_max_yd:g} yd and >= {min_move_yd:g} yd moved);"
                     f" last action {b1.get('last_action')!r}")
    else:
        ev.append(walk_line)
    if follow_after is None and not fails:
        return Outcome(INCONCLUSIVE, ev + ["no out-of-combat sample after the fight: strategy list not checked"])
    return Outcome(FAIL if fails else PASS, fails + ev,
                   {"moved_yd": round(moved, 1), "final_yd": round(final, 1), "follow_after": follow_after})


def evaluate_healer_follows(samples: list[dict], healer: str, t_walk_end: float, end_max_yd: float = 15,
                            stall_s: float = 20, actions=(REACH_HEAL,)) -> Outcome:
    """Out of combat, after a straight walk the healer ends within end_max_yd; a standstill of stall_s or more
    with "reach party member to heal" fails (repro 129: 70 s frozen at 38 yd, party at 100 %)."""
    fails, ev = [], []
    runs = [r for r in standstill_runs(samples, healer, actions) if r["seconds"] >= stall_s]
    for r in runs:
        fails.append(f"{healer} stood still {r['seconds']:g} s at {_xy(r)} with {r['action']!r}"
                     f" (t {r['start']:g}-{r['end']:g} s)")
    end = [s for s in samples if s["t"] >= t_walk_end and leader_dist(s, healer) is not None]
    if not end:
        return Outcome(FAIL if fails else INCONCLUSIVE, fails + ["no healer sample after the walk"])
    final = leader_dist(end[-1], healer)
    worst = max(d for s in samples if (d := leader_dist(s, healer)) is not None)
    line = f"{healer} ended {final:.1f} yd from the leader (max {worst:.1f} yd), last action {bot_of(end[-1], healer).get('last_action')!r}"
    if final > end_max_yd:
        fails.append(line + f" - over {end_max_yd:g} yd")
    else:
        ev.append(line)
    longest = _longest(standstill_runs(samples, healer, actions))
    return Outcome(FAIL if fails else PASS, fails + ev,
                   {"final_yd": round(final, 1), "max_yd": round(worst, 1),
                    "longest_reach_standstill_s": longest["seconds"] if longest else 0})


def evaluate_ranged_reach(samples: list[dict], casters: list[str], standstill_s: float = 10,
                          actions=(REACH_SPELL, REACH_HEAL), fought: bool | None = None,
                          records: list[dict] | None = None) -> Outcome:
    """During a tank pull no caster stands still standstill_s or more with "reach spell" / "reach party member
    to heal" (that action means the target is out of range). Repro 124: the mage stood 70 s at ~31 yd.
    records (on the samples' clock) tell when each caster's casts went off: a caster that keeps casting from its
    spot is not stalled, and only its cast-free stretches count (audit 2026-10-13: 14.5 s of Icicles, no stall)."""
    if fought is None:
        fought = any(in_combat(b) for s in samples for b in (s.get("bots") or {}).values())
    if not fought:
        return Outcome(INCONCLUSIVE, ["no bot entered combat: the pull started no fight"])
    fails, ev, metrics = [], [], {}
    for n in casters:
        casts = cast_times(records, n) if records is not None else None
        runs = standstill_runs(samples, n, actions, casts=casts)
        longest = _longest(runs)
        metrics[n] = longest["seconds"] if longest else 0
        bad = [r for r in runs if r["seconds"] >= standstill_s]
        for r in bad:
            fails.append(f"{n} stood still {r['seconds']:g} s at {_xy(r)} with {r['action']!r} (t {r['start']:g}-{r['end']:g} s)")
        if not bad:
            ev.append(f"{n}: longest reach standstill {metrics[n]:g} s (limit {standstill_s:g} s)"
                      + (f"; {len(casts)} casts in the window" if casts else ""))
    return Outcome(FAIL if fails else PASS, fails + ev, {"longest_standstill_s": metrics})


def _not_full(b: dict) -> bool:
    """Below full HP, or below full mana (only mana users). A figure the sample lacks is skipped, not read as 0."""
    if (b.get("max_hp") or 0) and b.get("hp") is not None and float(b["hp"]) < float(b["max_hp"]):
        return True
    return ((b.get("power_type") or "mana") == "mana" and (b.get("max_power") or 0) > 0
            and b.get("power") is not None and float(b["power"]) < float(b["max_power"]))


def _eating(b: dict) -> bool:
    """Eating or drinking: a resting bot finishes before it follows."""
    return _action(b) in ("drink", "food", "eat") or b.get("state") == "resting"


def evaluate_moon_sheep(records: list[dict], mage: str, moon: dict, t_pull: float, within_s: float = 10,
                        moon_engaged: bool | None = None, mage_guid: int | None = None,
                        moon_combat_t: float | None = None) -> Outcome:
    """The first Polymorph on the Moon mob completes within within_s of the pull, and no party damage lands
    on the Moon mob before it is sheeped. records (records_from_events / records_from_lines) start at the
    cc order: a sheep cast between the order and the pull counts (negative seconds). mage_guid: the
    dashboard's low guid, matched against source_guid when the event carries no name.
    b11-focus: first completion 26 s after the pull, 11 starts, party hit the Moon mob throughout.

    within_s counts from the Moon mob's own entry into combat (the owner's 10 s hold; the mage cannot sheep
    a mob that is not fighting yet): the earliest non-Polymorph record with the Moon mob as source or target
    (an attack_start, a hit, a spell) at or after the pull order, or moon_combat_t (epoch seconds of the
    first poll that saw it in combat), whichever is first. Without a guid (a recorded real-client log) the
    pull order stands in. The pull-anchored times stay as information. Never entering combat: INCONCLUSIVE."""
    def on_moon(r):
        if moon.get("guid") and r.get("target_guid"):
            return str(r["target_guid"]).lower() == str(moon["guid"]).lower()
        return (r.get("target") or "").lower() == str(moon.get("name") or "").lower()

    def is_mage(r):
        if (r.get("source") or "").lower() == mage.lower():
            return True
        g = r.get("source_guid")
        return bool(mage_guid and g) and (int(str(g), 16) & 0xFFFFFFFF) == int(mage_guid)

    by_mage = [r for r in records if r["poly"] and is_mage(r)]
    starts = [r for r in by_mage if r["kind"] == "start"]
    gos = [r for r in by_mage if r["kind"] == "go"]
    # a spell_go followed (before the mage's next go, within 2 s) by a miss record on the Moon mob was resisted,
    # missed or immune: it did not sheep. The aura-applied record is the stronger signal and is used as well.
    misses = [r for r in records if r["kind"] == "miss" and r["poly"] and on_moon(r)]

    def resisted(g):
        nxt = next((x["t"] for x in gos if x["t"] > g["t"]), float("inf"))
        return any(g["t"] - 0.05 <= m["t"] < min(nxt, g["t"] + 2.0) for m in misses)

    gos_moon = [r for r in gos if on_moon(r) and not resisted(r)]
    auras = [r for r in records if r["kind"] == "aura" and r["poly"] and on_moon(r)]
    first_go = gos_moon[0]["t"] if gos_moon else None
    sheeped = min([t for t in (first_go, auras[0]["t"] if auras else None) if t is not None], default=None)
    hits = [r for r in records if r["kind"] == "damage" and r.get("party") and on_moon(r)]
    early = [r for r in hits if r["t"] < t_pull - 0.5]
    pre = [r for r in hits if r["t"] >= t_pull - 0.5 and (sheeped is None or r["t"] < sheeped)]
    def of_moon(r):
        g = str(moon.get("guid") or "").lower()
        return bool(g) and g in (str(r.get("source_guid") or "").lower(), str(r.get("target_guid") or "").lower())

    seen = [r["t"] for r in records if not r["poly"] and r["kind"] in ("combat", "damage", "start", "go")
            and r["t"] >= t_pull - 0.5 and of_moon(r)]
    seen += [moon_combat_t] if moon_combat_t is not None else []
    t_moon = min(seen) if seen else (None if moon.get("guid") else t_pull)
    metrics = {"polymorph_starts": len(starts), "polymorph_completions": len(gos),
               "completions_on_moon": len(gos_moon),
               "first_start_s": round(starts[0]["t"] - t_pull, 1) if starts else None,
               "first_completion_s": round(first_go - t_pull, 1) if first_go is not None else None,
               "party_hits_before_sheep": len(pre),
               "moon_combat_s": round(t_moon - t_pull, 1) if t_moon is not None else None,
               "first_completion_after_moon_combat_s": round(first_go - t_moon, 1)
               if first_go is not None and t_moon is not None else None}
    ev = [f"Polymorph by {mage}: {len(starts)} starts, {len(gos)} completions ({len(gos_moon)} on the Moon mob)"]
    if early:
        return Outcome(INCONCLUSIVE, [f"the Moon mob took party damage before the pull: {_rline(early[0], t_pull)}"] + ev, metrics)
    after = [r for r in records if r["t"] >= t_pull - 0.5]
    if not after:
        return Outcome(INCONCLUSIVE, ["no combat records after the pull order: the pull started no fight"] + ev, metrics)
    if moon_engaged is False and not starts:
        return Outcome(INCONCLUSIVE, ["the Moon mob never entered combat and was never targeted: nothing to sheep"] + ev, metrics)
    if t_moon is None:
        return Outcome(INCONCLUSIVE, ["the Moon mob never entered combat (no event or poll shows it fighting): "
                                      "its 10 s hold never started"] + ev, metrics)
    fails = []
    if t_moon != t_pull:
        ev.append(f"the Moon mob entered combat {t_moon - t_pull:.1f} s after the pull order (the {within_s:g} s counts from there)")
    if first_go is None:
        fails.append(f"no Polymorph completed on the Moon mob ({moon.get('name')}) after the pull")
    elif first_go - t_moon > within_s:
        fails.append(f"first Polymorph on the Moon mob completed {first_go - t_moon:.1f} s after it entered combat"
                     f" ({first_go - t_pull:.1f} s after the pull; limit {within_s:g} s)")
    else:
        ev.append(f"first Polymorph on the Moon mob completed {first_go - t_moon:.1f} s after it entered combat"
                  f" ({first_go - t_pull:.1f} s after the pull)"
                  + (" (cast at the cc order, before the pull)" if first_go < t_pull else ""))
    if pre:
        fails.append(f"{len(pre)} party hit(s) on the Moon mob before it was sheeped; first: {_rline(pre[0], t_pull)}")
        fails += [f"  {_rline(r, t_pull)}" for r in pre[1:4]]
    else:
        ev.append("no party damage on the Moon mob before the sheep")
    return Outcome(FAIL if fails else PASS, fails + ev, metrics)


def _rline(r: dict, t0: float | None = None) -> str:
    if r.get("line"):
        return r["line"]
    when = f"+{r['t'] - t0:.1f} s " if t0 is not None else ""
    spell = f" spell {r['spell_id']}" if r.get("spell_id") else " melee"
    return f"{when}{r.get('source')} -> {r.get('target')}{spell} {r.get('amount') or ''}".rstrip()


def evaluate_no_jumps(samples: list[dict], map_id: int = 70, min_samples: int = 10) -> Outcome:
    """No bot's last_action contains "jump" while it is on map_id (#125: no jumps in dungeons)."""
    seen, hits, keys = 0, [], set()
    for s in samples:
        for b in (s.get("bots") or {}).values():
            if b.get("map") != map_id:
                continue
            seen += 1
            key = (s.get("scenario"), s["t"], b.get("name"))
            if "jump" in _action(b) and key not in keys:
                keys.add(key)
                where = f"{s['scenario']} " if s.get("scenario") else ""
                hits.append(f"{where}t {s['t']:g} s {b.get('name')} at {_xy(b)}: {b.get('last_action')!r}")
    if hits:
        return Outcome(FAIL, [f"{len(hits)} jump sample(s) on map {map_id}"] + hits[:8],
                       {"dungeon_bot_rows": seen, "jumps": len(hits)})
    if seen < min_samples:
        return Outcome(INCONCLUSIVE, [f"only {seen} bot rows on map {map_id} (need {min_samples})"], {"dungeon_bot_rows": seen})
    return Outcome(PASS, [f"{seen} bot rows on map {map_id}, no jump action"], {"dungeon_bot_rows": seen, "jumps": 0})


# ---------------------------------------------------------------- combat records

def iso_epoch(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def clock_seconds(s: str) -> float:
    """'10/12 13:44:46.625' or '13:44:36' as seconds since midnight (the recorded logs' clock)."""
    hms = s.split()[-1]
    h, m, sec = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def records_from_events(evs: list[dict], party_names=(), poly_ids=POLYMORPH_IDS) -> list[dict]:
    """turtle-agent combat events as the records evaluate_moon_sheep reads (t = epoch seconds)."""
    party = {n.lower() for n in party_names}
    out = []
    for e in evs:
        k, d = e.get("kind"), e.get("data") or {}
        kind = {"spell_start": "start", "spell_go": "go", "aura_applied": "aura", "spell_damage": "damage",
                "attack_start": "combat", "spell_miss": "miss"}.get(k)
        if k == "melee" and d.get("outcome", "normal") == "normal":
            kind = "damage"
        elif k == "periodic" and d.get("tick") == "damage":
            kind = "damage"
        if kind is None or not e.get("time"):
            continue
        src = e.get("source") or ""
        out.append({"t": iso_epoch(e["time"]), "kind": kind, "source": src, "source_guid": e.get("source_guid"),
                    "target": e.get("target") or "", "target_guid": e.get("target_guid"),
                    "poly": e.get("spell_id") in poly_ids, "amount": e.get("amount") or 0, "spell_id": e.get("spell_id"),
                    "party": bool(e.get("party")) or src.lower() in party, "line": None,
                    "cast_ok": k in ("spell_go", "spell_damage")})
    return sorted(out, key=lambda r: r["t"])


def records_from_lines(lines: list[str], me: str, party_names=()) -> list[dict]:
    """1.12 combat-log lines (a recorded real-client run) as records (t = seconds since midnight)."""
    party = {n.lower() for n in party_names} | {me.lower()}
    kinds = {"cast_start": "start", "cast": "go", "aura": "aura", "melee": "damage", "spell": "damage", "periodic": "damage"}
    out = []
    for e in parse_lines(lines, me):
        kind = kinds.get(e.kind)
        if kind is None or not e.time:
            continue
        out.append({"t": clock_seconds(e.time), "kind": kind, "source": e.source, "source_guid": None,
                    "target": e.target or "", "target_guid": None, "poly": (e.spell or "").startswith("Polymorph"),
                    "amount": e.amount or 0, "party": (e.source or "").lower() in party, "line": e.raw,
                    "cast_ok": e.kind in ("cast", "spell")})
    return sorted(out, key=lambda r: r["t"])


def align(records: list[dict], t0: float) -> list[dict]:
    """Records shifted onto a clock that starts at t0 (epoch or seconds since midnight)."""
    return [{**r, "t": round(r["t"] - t0, 3)} for r in records]


# ---------------------------------------------------------------- config

def _check_spot(spot, where: str) -> dict:
    if not isinstance(spot, dict):
        raise ValueError(f"{where}: a spot is an object, got {spot!r}")
    if "tele" in spot:
        if not re.fullmatch(r"[A-Za-z0-9_' ]+", str(spot["tele"])):
            raise ValueError(f"{where}: bad tele name {spot['tele']!r}")
        return spot
    for k in ("x", "y", "z", "map"):
        if not isinstance(spot.get(k), (int, float)):
            raise ValueError(f"{where}: spot needs numeric x, y, z and map (or a tele name)")
    return spot


def _no_secrets(node, path="config"):
    if isinstance(node, dict):
        for k, v in node.items():
            if SECRET_KEYS.search(str(k)):
                raise ValueError(f"{path}.{k}: the scenario config holds no credentials (turtle-agent keeps its own)")
            _no_secrets(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _no_secrets(v, f"{path}[{i}]")


def load_gate_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return check_gate_config(raw)


def check_gate_config(raw: dict) -> dict:
    """Validate and fill in defaults. Spots: {"tele": name} or {"x","y","z","map"}."""
    _no_secrets(raw)
    cfg = {**GATE_DEFAULTS, **{k: v for k, v in raw.items() if k != "scenarios"}}
    dg = cfg.get("dungeon") or {}
    if not isinstance(dg.get("map"), int):
        raise ValueError("dungeon.map (int) is required")
    _check_spot(dg.get("enter"), "dungeon.enter")
    _check_spot(cfg.get("safe"), "safe")
    _check_spot(cfg.get("start"), "start")
    party = cfg.get("party") or []
    if not party or any(not {"key", "class", "hire"} <= set(p) for p in party):
        raise ValueError("party: a list of {key, class, hire}")
    keys = {p["key"] for p in party}
    scs = {}
    for name in ORDER:
        sc = {**DEFAULTS[name], **((raw.get("scenarios") or {}).get(name) or {})}
        for k, v in sc.items():
            if k.endswith("spot") or k in ("walk_to", "pull_at", "pack_at", "mob_at", "start_spot", "end_spot"):
                _check_spot(v, f"scenarios.{name}.{k}")
        for k in ("bot", "healer", "mage"):
            if k in sc and sc[k] not in keys:
                raise ValueError(f"scenarios.{name}.{k}: {sc[k]!r} is not a party key {sorted(keys)}")
        for k in sc.get("casters", []):
            if k not in keys:
                raise ValueError(f"scenarios.{name}.casters: {k!r} is not a party key")
        for k in ("route",):
            for i, p in enumerate(sc.get(k, [])):
                _check_spot(p, f"scenarios.{name}.{k}[{i}]")
        if sc["budget_s"] > 200:
            raise ValueError(f"scenarios.{name}.budget_s: keep a scenario under ~3 minutes")
        if not 0 < cfg["setup_budget_s"] <= 300:
            raise ValueError("setup_budget_s: a scenario's setup cap, 1-300 s")
        scs[name] = sc
    need = {"stay_then_order_follows": ["spot", "walk_to"], "healer_follows_ooc": ["start_spot", "end_spot"],
            "ranged_reach_in_dungeon": ["spot", "pull_at"], "moon_sheep_lands": ["spot", "pack_at"],
            "no_jumps_in_dungeon": ["route"]}
    for name, ks in need.items():
        for k in ks:
            if k not in scs[name]:
                raise ValueError(f"scenarios.{name}.{k} is required")
    cfg["scenarios"] = scs
    return cfg


def spot_text(spot: dict) -> str:
    if "tele" in spot:
        return f".tele {spot['tele']}"
    return f"({spot['x']:.1f}, {spot['y']:.1f}, {spot['z']:.1f}) map {spot['map']}"


# ---------------------------------------------------------------- the runner

class Recording:
    def __init__(self, t0: float):
        self.t0, self.samples, self.dash_errors, self.extra = t0, [], 0, {}


class Gate:
    def __init__(self, client, dashboard, config: dict, clock=time.monotonic, sleep=time.sleep,
                 wall=time.time, log=None):
        self.c, self.d, self.cfg = client, dashboard, config
        self._clock, self._sleep, self._wall = clock, sleep, wall
        self.log = log or (lambda msg: None)
        self.gm = None  # unknown until the gate sets it
        self.roles: dict[str, str] = {}
        self.me = ""
        self.all_samples: list[dict] = []
        self.summons: list[dict] = []
        self.deadline = float("inf")
        self.in_setup = False  # the running scenario is still setting up (deadline = its setup cap)
        self.scenario_budget: float | None = None
        self.gate_end = float("inf")
        self.setup_s: float | None = None  # how long the running scenario's setup took
        self._scenario_t0: float | None = None
        self.actions: list[str] = []  # every command the gate sent, for the report
        self.current = ""  # the scenario running, tags the samples
        self.setup_log: list[str] = []  # the setup waits and steps of the scenario running, for its evidence

    # -- primitives
    def now(self) -> float:
        return self._clock()

    def wait(self, s: float) -> None:
        if s > 0:
            self._sleep(s)

    def _check_budget(self):
        if self.now() > self.deadline:
            raise BudgetExceeded(f"scenario setup time cap ({self.cfg['setup_budget_s']:g} s) used up" if self.in_setup
                                 else "scenario time budget used up")

    def setup_ready(self, t0: float | None = None) -> None:
        """The party is ready: the scenario's own budget starts now (capped by the gate's end)."""
        if not self.in_setup:
            return
        self.in_setup = False
        if self.scenario_budget is not None:
            self.deadline = min(self.now() + self.scenario_budget, self.gate_end)
        if t0 is not None:
            self.setup_s = round(self.now() - t0, 1)
            self.note(f"setup ready after {self.setup_s:.1f} s; the scenario budget of {self.scenario_budget:g} s starts now")

    def waited(self, what: str, since: float, then: str = "", record: bool = True) -> float:
        """Log one setup wait: what it was for and how long. A wait of a second or more also goes into the
        scenario's evidence (record), so the report can say why the scenario started late."""
        secs = round(self.now() - since, 1)
        line = f"wait: {what} after {secs:.1f} s{then}"
        self.log(line)
        if record and secs >= 1:
            self.setup_log.append(line)
        return secs

    def note(self, msg: str) -> None:
        """A setup step that is not a timed wait (a mob selected, a `.die`): logged and kept in the evidence."""
        self.log(msg)
        self.setup_log.append(msg)

    def call(self, method: str, **params):
        return self.c.call(method, **params)

    def cmd(self, text: str) -> None:
        """A GM / .bot command (no reply wait: replies are read from the event log where they matter)."""
        self.actions.append(text)
        self.log(f"> {text}")
        self.call("gm", text=text)

    def whisper(self, name: str, text: str) -> None:
        self.actions.append(f"/w {name} {text}")
        self.log(f"> /w {name} {text}")
        self.call("chat", type="whisper", to=name, text=text)

    def gm_on(self) -> None:
        self.cmd(".gm on")
        self.gm = True

    def gm_off(self) -> None:
        """The only way the gate turns GM off: GM on, teleport to the safe spot, then `.gm off`."""
        self.gm_on()
        self.goto(self.cfg["safe"])
        self.cmd(".gm off")
        self.gm = False

    def status(self) -> dict:
        return self.call("status")

    def last_seq(self) -> int:
        return int(self.status().get("last_seq") or 0)

    def goto(self, spot: dict, wait_s: float = 10.0) -> dict:
        """Teleport to a spot and check where the player ended up (a bad z can drop it under the map)."""
        self._check_budget()
        if "tele" in spot:  # done when turtle-agent logs the server's teleport (same map or a new one)
            since, seq = self.now(), self.last_seq()
            self.cmd(f".tele {spot['tele']}")
            end = self.now() + wait_s
            while self.now() < end:
                self.wait(0.5)
                moved = [e for e in self.events_since(seq, ["session"])
                         if (e.get("data") or {}).get("state") in ("teleported", "new_world")]
                st = self.status()
                if moved and ("map" not in spot or st.get("map") == spot["map"]):
                    self.wait(1.0)
                    self.waited(f"teleport to {spot_text(spot)}", since, record=False)  # routine: logged, not in evidence
                    return self.status()
            raise ScenarioSetupError(f".tele {spot['tele']}: no teleport within {wait_s:g} s")
        self.actions.append(f"teleport {spot_text(spot)}")
        self.call("teleport", x=spot["x"], y=spot["y"], z=spot["z"], map=spot["map"])
        self.wait(1.0)
        st = self.status()
        off = math.dist((st.get("x", 0), st.get("y", 0), st.get("z", 0)), (spot["x"], spot["y"], spot["z"]))
        if st.get("map") != spot["map"] or off > self.cfg["teleport_tolerance_yd"]:
            raise ScenarioSetupError(f"teleport to {spot_text(spot)} ended at ({st.get('x', 0):.1f}, {st.get('y', 0):.1f},"
                                     f" {st.get('z', 0):.1f}) map {st.get('map')}")
        return st

    def sample(self, rec: Recording, names: list[str]) -> dict:
        st = self.status()
        lead = {k: st.get(k) for k in ("map", "x", "y", "z", "health", "max_health")}
        try:
            bots = {str(b.get("name", "")).lower(): b for b in self.d.bots(names)}
        except DashboardUnavailable:
            rec.dash_errors += 1
            bots = {}
        s = {"t": round(self.now() - rec.t0, 2), "leader": lead, "bots": bots, "scenario": self.current}
        rec.samples.append(s)
        self.all_samples.append(s)
        return s

    def observe(self, rec: Recording, names: list[str], seconds: float, until=None, each=None) -> None:
        """Sample every sample_s for `seconds` (or to the budget), stopping early once until(rec) is true;
        each(sample) runs after every sample (extra reads such as the Moon mob's combat flag)."""
        end = min(self.now() + seconds, self.deadline)
        while True:
            s = self.sample(rec, names)
            if each:
                each(s)
            if (until and until(rec)) or self.now() >= end:
                return
            self.wait(min(self.cfg["sample_s"], max(end - self.now(), 0)))

    def walk(self, rec: Recording, names: list[str], points: list[dict]) -> None:
        """walk_to along straight legs of at most leg_yd, sampling after each leg."""
        for p in points:
            st = self.status()
            if st.get("map") != p["map"]:
                raise ScenarioSetupError(f"walk target {spot_text(p)} is on another map than the player ({st.get('map')})")
            d = math.dist((st["x"], st["y"], st["z"]), (p["x"], p["y"], p["z"]))
            n = max(1, math.ceil(d / self.cfg["leg_yd"]))
            for i in range(1, n + 1):
                self._check_budget()
                f = i / n
                x, y, z = (st["x"] + (p["x"] - st["x"]) * f, st["y"] + (p["y"] - st["y"]) * f, st["z"] + (p["z"] - st["z"]) * f)
                self.call("walk_to", x=x, y=y, z=z)
                self.sample(rec, names)
            self.actions.append(f"walk to {spot_text(p)} ({d:.0f} yd, {n} legs)")
            end = self.status()
            if dist2d(end, p) > 3:
                raise ScenarioSetupError(f"walk ended at {_xy(end)}, not at {spot_text(p)}")

    def units(self, **params) -> list[dict]:
        return self.call("units", **params) or []

    def summon(self, entry: int) -> dict:
        """`.npc summon <entry>` at the player; returns the new creature (its guid is tracked for cleanup)."""
        before = {u["guid"] for u in self.units(entry=entry, near=15, limit=50)}
        self.cmd(f".npc summon {entry}")
        for _ in range(10):
            self.wait(0.5)
            new = [u for u in self.units(entry=entry, near=15, limit=50) if u["guid"] not in before and not u.get("dead")]
            if new:
                self.summons.append(new[0])
                return new[0]
        raise ScenarioSetupError(f".npc summon {entry}: no new creature appeared")

    def select(self, guid: str) -> bool:
        """Target a guid and confirm the selection (guards `.die` and `.modify hp`)."""
        self.call("target", guid=guid)
        self.wait(1.0)  # the server can run a chat command before the selection packet (live: wrong target)
        return self.selected(guid)

    def selected(self, guid: str) -> bool:
        """The player's selection is this guid right now (no new target sent)."""
        g = guid_int(guid)
        return g is not None and guid_int(self.status().get("target")) == g

    def kill_leftovers(self) -> list[str]:
        """Kill creatures this gate summoned that are still alive; `.die` only on a confirmed selection of that same
        guid, a creature (0xF130) outside the party, with GM on (turned on first when it is not)."""
        notes = []
        for u in list(self.summons):
            alive = [x for x in self.units(entry=u.get("entry") or 0, near=200, limit=100)
                     if str(x["guid"]).lower() == str(u["guid"]).lower() and not x.get("dead")]
            if not alive:
                self.summons.remove(u)
                continue
            g, row = guid_int(u["guid"]), alive[0]
            if (g is None or g >> 48 != HIGH_CREATURE or row.get("type") != "creature" or row.get("in_party")
                    or g in self.party_guids()):
                notes.append(f"leftover {u.get('name')} {u['guid']} is not a creature outside the party: left alive")
                self.summons.remove(u)
                continue
            if self.gm is not True:  # never a .die with GM off: GM on is the safe direction
                self.gm_on()
            if self.select(u["guid"]) and self.gm is True and self.selected(u["guid"]):
                self.cmd(".die")
                notes.append(f"killed leftover {u.get('name')} {u['guid']}")
                self.summons.remove(u)
            else:
                notes.append(f"could not select leftover {u.get('name')} {u['guid']}: left alive")
        return notes

    def replies(self, seq: int, names: list[str], limit: int = 6) -> list[str]:
        """Bot whispers and TBM:/system lines since seq that answer an order (evidence for the report)."""
        from .headless import chat_line
        want = {n.lower() for n in names}
        out = [chat_line(e).split("  ", 1)[-1] for e in self.events_since(seq, CHAT_KINDS)
               if (e.get("source") or "").lower() in want or "TBM:" in (e.get("text") or "")
               or any(n in (e.get("text") or "").lower() for n in want)]
        return [l for l in out if not l.startswith("To [")][-limit:]

    def poll_until(self, read, until: float) -> list[str]:
        """read() until it returns something or the clock reaches `until`. A bot's reply can arrive more than 4 s after
        the order, so one read at a fixed time can miss it; this polls until the reply shows. Use it wherever a
        pass/fail decision or a verdict note rests on a reply."""
        while True:
            self._check_budget()
            got = read()
            if got or self.now() >= until:
                return got
            self.wait(0.5)

    def whispers_from(self, seq: int, name: str, until: float) -> list[str]:
        """The whisper lines `name` sent since seq, read until one is in the event log or the clock reaches `until`."""
        return self.poll_until(lambda: [l for l in self.replies(seq, [name], limit=50) if "whispers:" in l], until)

    def acks_from(self, seq: int, until: float) -> list[str]:
        """The TBM:ACTION_ACK / TBM:ACTION_ERR texts since seq, read until one is in the event log or `until`."""
        return self.poll_until(lambda: [e.get("text") or "" for e in self.events_since(seq, CHAT_KINDS)
                                        if "TBM:ACTION" in (e.get("text") or "")], until)

    def mark_events(self, mark: str) -> list[dict]:
        """Every raid_mark event of the icon since the log began, oldest first (turtle-agent logs a `set` for the unit
        that got the icon and a `cleared` for the one that lost it, and nothing when the icon's holder did not change)."""
        return [e for e in self.events_since(0, ["raid_mark"]) if (e.get("data") or {}).get("icon") == mark]

    @staticmethod
    def mark_holder(events: list[dict]) -> int | None:
        """Who holds the icon after these events: the guid of the last `set`, None after a `cleared` of that holder or
        when nothing was ever logged (unknown)."""
        holder = None
        for e in events:
            g, action = guid_int(e.get("target_guid")), (e.get("data") or {}).get("action")
            if action == "set":
                holder = g
            elif action == "cleared" and g == holder:
                holder = None
        return holder

    @staticmethod
    def cc_reply_of(texts: list[str], mark: str, mage: str) -> str | None:
        """The TBM:ACTION reply of `.bot action cc <mark> <mage>` among these texts. The server's lines are
        `TBM:ACTION_ACK|<intent>|<scope>|<count>|<executor>` and `TBM:ACTION_ERR|<intent>|<code>|<message>`, where the
        intent is the order's words: `cc moon Khikraco` (live), or `cc moon` when no bot was named. An ACK whose scope
        is `bot:<name>` must name this mage. None when no line answers this order."""
        intents, bot = {f"cc {mark}".lower(), f"cc {mark} {mage}".lower()}, f"bot:{mage}".lower()
        for t in texts:
            f = t.strip().split("|")
            if len(f) < 2 or f[0] not in ("TBM:ACTION_ACK", "TBM:ACTION_ERR") or f[1].strip().lower() not in intents:
                continue
            if f[0] == "TBM:ACTION_ACK" and len(f) > 2 and f[2].lower().startswith("bot:") and f[2].lower() != bot:
                continue
            return t
        return None

    def cc_reply(self, seq: int, mark: str, mage: str, until: float) -> str | None:
        """The reply of this cc order (sent after seq), polled until it shows or the clock reaches `until`."""
        got = self.poll_until(lambda: [r] if (r := self.cc_reply_of(self.acks_from(seq, self.now()), mark, mage))
                              else [], until)
        return got[0] if got else None

    def order_mark(self, mark: str, mage: str, target: dict) -> int:
        """`.bot action cc <mark> <mage>` on `target`, which is selected. The selection packet and the chat command run
        on separate server queues, so the order can act on the previous selection (live: the moon mark landed on a
        dead mob). The order counts when the icon ends up on the target's guid and the server did not refuse it:
        turtle-agent logs a raid_mark only when the icon's holder changes, so a mark that is already on the target logs
        nothing (and a stale-selection order logs `cleared` for the old holder and `set` for the wrong unit). The holder
        is replayed from the log; with a change, it must be the target; with no change within MARK_WAIT_S, it must
        already have been the target and the order's own TBM:ACTION_ACK must come (an order on a dead selection is
        refused with TBM:ACTION_ERR|cc <mark> <mage>|no-target|... and moves no mark, so a mark left from an earlier run proves
        nothing). A TBM:ACTION_ERR for the order is a miss even when a `set` row reached the target: the server sets
        the icon before it checks the executor, so the ERR is the one that says whether the assignment was saved.
        On a miss the target is selected again and, unless the mark has landed in the meantime (a row after the first
        order and no ERR for it), the order sent once more. A second miss is a ScenarioSetupError (INCONCLUSIVE)
        naming where the mark landed or the ERR text. Returns the event seq just before the order that landed (where
        its TBM:ACTION reply is read from)."""
        want, misses = guid_int(target["guid"]), []
        seq = self.last_seq()
        for attempt in (1, 2):
            if attempt == 2:
                self.note(f"cc {mark}: {misses[0]}; selecting {target.get('name')} {target['guid']} again")
                if not self.select(target["guid"]):
                    raise ScenarioSetupError(f"could not select {target.get('name')} again; {misses[0]}")
                evs = self.mark_events(mark)
                first = self.cc_reply_of(self.acks_from(seq, self.now()), mark, mage) or ""
                if (any(e["seq"] > seq for e in evs) and self.mark_holder(evs) == want
                        and "TBM:ACTION_ERR" not in first):  # it landed late: no second order needed
                    self.note(f"cc {mark}: the mark reached {target.get('name')} late; not resent")
                    return seq
                self.note(f"cc {mark}: resending")
                seq = self.last_seq()
            before = self.mark_holder([e for e in self.mark_events(mark) if e["seq"] <= seq])
            self.cmd(f".bot action cc {mark} {mage}")
            t_order = self.now()
            until = t_order + MARK_WAIT_S
            while True:
                self._check_budget()
                evs = self.mark_events(mark)
                changed = any(e["seq"] > seq for e in evs)
                holder = self.mark_holder(evs)
                if changed or self.now() >= until:
                    break
                self.wait(0.5)
            landed = (changed and holder == want) or (not changed and before == want)
            # the reply decides only when the mark is right; otherwise it is read once, as evidence
            reply = self.cc_reply(seq, mark, mage, t_order + ACK_REPLY_WAIT_S if landed else self.now())
            refused = reply is not None and "TBM:ACTION_ERR" in reply
            if landed and not refused and (changed or reply is not None):
                return seq
            if refused:
                misses.append(f"the server refused the cc {mark} order: {reply}"
                              f" (the {mark} icon is on {self.mark_name(evs, holder)})")
            elif landed:  # no change, the icon already on the target, and no reply to say the order ran
                misses.append(f"no raid_mark event and no TBM:ACTION reply within {ACK_REPLY_WAIT_S:g} s of the cc"
                              f" {mark} order (the icon was already on {target.get('name')} {target['guid']})")
            else:
                misses.append(f"the {mark} mark landed on {self.mark_name(evs, holder)}, not on {target.get('name')} {target['guid']}"
                              if changed else f"no raid_mark event for the {mark} icon within {MARK_WAIT_S:g} s of the cc order"
                              f" (it was held by {('0x%X' % before) if before else 'nobody known'})")
        raise ScenarioSetupError(f"cc {mark} order missed {target.get('name')} twice: " + "; then ".join(misses))

    @staticmethod
    def mark_name(events: list[dict], holder: int | None) -> str:
        if holder is None:
            return "nobody (the mark was cleared)"
        e = next((e for e in reversed(events) if guid_int(e.get("target_guid")) == holder), {})
        return f"{e.get('target') or '?'} {e.get('target_guid')}"

    def order_refused(self, seq: int) -> bool:
        """A TBM:ACTION_ERR or "could not execute" answer since seq: the order will not start a fight."""
        return any("TBM:ACTION_ERR" in (e.get("text") or "") or "could not execute" in (e.get("text") or "")
                   for e in self.events_since(seq, ["system"]))

    def events_since(self, seq: int, kinds: list[str]) -> list[dict]:
        out = []
        while True:
            page = self.call("events", since=seq, kinds=kinds, limit=5000)
            out += page.get("events") or []
            seq = page.get("next", seq)
            if not page.get("more"):
                return out

    # -- party and placement
    def party_names(self) -> list[str]:
        return [m["name"] for m in (self.call("party").get("members") or []) if m.get("name")]

    def party_guids(self) -> set[int]:
        """The leader's and every party member's guid, read fresh (status and party)."""
        out = {guid_int(m.get("guid")) for m in (self.call("party").get("members") or [])}
        out.add(guid_int(self.status().get("guid")))
        out.discard(None)
        return out

    def resolve_party(self) -> tuple[dict, list[dict]]:
        """Map each config party entry to a party member of that class (dashboard class field)."""
        names = self.party_names()
        try:
            bots = self.d.bots(names) if names else []
        except DashboardUnavailable:
            bots = []
        by_class: dict[str, list[str]] = {}
        for b in bots:
            by_class.setdefault(str(b.get("class", "")).lower(), []).append(b["name"])
        roles, missing = {}, []
        for p in self.cfg["party"]:
            pool = by_class.get(p["class"].lower()) or []
            if pool:
                roles[p["key"]] = pool.pop(0)
            else:
                missing.append(p)
        return roles, missing

    def ensure_party(self, wait_s: float = 40.0) -> dict:
        if self.roles and all(n in self.party_names() for n in self.roles.values()):
            return self.roles
        roles, missing = self.resolve_party()
        if missing:
            since, hires = self.now(), ", ".join(p["hire"] for p in missing)
            if self.cfg.get("hire_at"):
                self.gm_on()
                self.goto(self.cfg["hire_at"])
            for p in missing:
                self.cmd(f".bot hire {p['hire']}")
                self.wait(1.5)
            end = self.now() + wait_s
            while self.now() < end:
                self.wait(1.0)
                roles, missing = self.resolve_party()
                if not missing:
                    break
            if missing:
                raise ScenarioSetupError("hire did not complete: missing " + ", ".join(p["hire"] for p in missing))
            self.waited(f"party hired ({hires})", since)
        self.roles = roles
        return roles

    def settle_leader(self) -> None:
        """Wait until the leader is in place after a teleport: two readings one second apart show the same map and
        position. A summon of a bot on another map waits for this, since a summon during the arrival lands it off the
        spot."""
        since, prev = self.now(), None
        while True:
            st = self.status()
            here = (st.get("map"), round(float(st.get("x") or 0), 1), round(float(st.get("y") or 0), 1),
                    round(float(st.get("z") or 0), 1))
            if here == prev:
                self.waited(f"leader in place on map {here[0]} at ({here[1]:.1f}, {here[2]:.1f}, {here[3]:.1f})", since)
                return
            if self.now() - since >= self.cfg["settle_wait_s"]:
                self.waited(f"leader still moving (last reading {here[1]:.1f}, {here[2]:.1f}, {here[3]:.1f})", since)
                raise ScenarioSetupError(f"leader not in place within {self.cfg['settle_wait_s']:g} s:"
                                         " bots on another map not summoned")
            prev = here
            self._check_budget()
            self.wait(1.0)

    @staticmethod
    def _away_reason(s: dict, name: str, near_yd: float) -> str:
        b, lead_map = bot_of(s, name), (s.get("leader") or {}).get("map")
        if b is None:
            return f"{name} has no dashboard data"
        if in_combat(b):
            return f"{name} in combat"
        if b.get("state") == "dead" or not b.get("hp"):
            return f"{name} dead"
        if b.get("map") != lead_map:
            return f"{name} on map {b.get('map')}, leader on map {lead_map}"
        d = leader_dist(s, name)
        return f"{name} {d:.0f} yd away, limit {near_yd:g}" if d is not None else f"{name} away"

    def gather(self, names: list[str], near_yd: float | None = None, wait_s: float = 15.0,
               resend_s: float = 5.0, max_summons: int = 3) -> None:
        """Every bot alive, on the player's map and within near_yd; `.revive`/`.bot summon` the others. The summon is
        re-sent every resend_s (the short wait) to the ones still away, at most max_summons times per bot. A bot in
        combat refuses the summon, so it is skipped until it is out. A bot on another map is summoned only once the
        leader is in place (settle_leader). Every wait is logged; the failure names why each bot is away and quotes
        the server's replies."""
        near_yd = near_yd or self.cfg["gather_yd"]
        rec = Recording(self.now())
        seq0 = self.last_seq()
        since = self.now()
        sent: dict[str, int] = {}
        last_sent = 0.0

        def away(s):
            out = []
            for n in names:
                b, d = bot_of(s, n), leader_dist(s, n)
                if b is None or d is None or d > near_yd or (b.get("state") == "dead") or not b.get("hp"):
                    out.append(n)
            return out

        def summon(s, bad):
            nonlocal last_sent
            lead_map = (s.get("leader") or {}).get("map")
            if any((b := bot_of(s, n)) and b.get("map") != lead_map for n in bad):
                self.settle_leader()  # the summon teleports a bot on another map to where the leader stands
            for n in bad:
                b = bot_of(s, n)
                if b and in_combat(b):
                    continue  # refused in combat: try again once it is out
                if sent.get(n, 0) >= max_summons:
                    continue
                if b and (b.get("state") == "dead" or not b.get("hp")):
                    self.cmd(f".revive {n}")
                    self.wait(1.0)
                self.cmd(f".bot summon {n}")
                sent[n] = sent.get(n, 0) + 1
                self.wait(1.5)
            last_sent = self.now()

        s = self.sample(rec, names)
        bad = away(s)
        if not bad:
            return
        summon(s, bad)
        end = self.now() + wait_s
        while self.now() < end:
            self._check_budget()
            self.wait(1.0)
            s = self.sample(rec, names)
            bad = away(s)
            if not bad:
                self.waited(f"bots with the leader within {near_yd:g} yd ({', '.join(names)}); summons sent:"
                            f" {sum(sent.values())}", since)
                return
            if self.now() - last_sent >= resend_s:
                summon(s, bad)
        reasons = "; ".join(self._away_reason(s, n, near_yd) for n in bad)
        self.waited(f"bots still away ({reasons}); summons sent: {sum(sent.values())}", since)
        try:
            said = self.replies(seq0, bad, limit=8)
        except Exception:  # the report still gets the main message
            said = []
        raise ScenarioSetupError(f"bots not with the player after summon: {', '.join(bad)}"
                                 f" (summons sent: {sum(sent.values())}; {reasons})"
                                 + (" | server: " + " / ".join(said) if said else " | no server reply seen"))

    def _calm_wait(self, names: list[str], wait_s: float, full_hp: bool = False) -> tuple[bool, list[dict | None]]:
        """Poll the dashboard until every named bot is out of combat (and at full HP with full_hp), for wait_s at
        most. Returns (whether it held, the last readings)."""
        rec = Recording(self.now())
        end = self.now() + wait_s
        while True:
            s = self.sample(rec, names)
            bots = [bot_of(s, n) for n in names]
            if all(b and not in_combat(b) for b in bots) and (
                    not full_hp or all(b.get("hp") and b.get("hp") >= b.get("max_hp", 0) for b in bots)):
                return True, bots
            if self.now() >= end:
                return False, bots
            self._check_budget()
            self.wait(1.0)

    @staticmethod
    def _states(names: list[str], bots: list[dict | None]) -> str:
        return ", ".join(f"{b.get('name')} {b.get('state')} {b.get('hp')}/{b.get('max_hp')}" if b
                         else f"{n} (no dashboard data)" for n, b in zip(names, bots))

    def wait_calm(self, names: list[str], wait_s: float, full_hp: bool = False) -> None:
        """Every named bot out of combat (and at full HP with full_hp) within wait_s. A fight still on after wait_s
        is finished (finish_fight), then the party gets a short re-check. Every wait is logged."""
        what = "out of combat at full health" if full_hp else "out of combat"
        since = self.now()
        ok, bots = self._calm_wait(names, wait_s, full_hp)
        if ok:
            self.waited(f"party {what} ({', '.join(names)})", since)
            return
        gone = [n for n, b in zip(names, bots) if b is None]
        if gone:
            raise ScenarioSetupError(f"no dashboard data for {', '.join(gone)} (party not {what} within {wait_s:g} s)")
        states = self._states(names, bots)
        if not any(in_combat(b) for b in bots):  # out of combat but short of HP: no fight to finish
            self.waited(f"party not {what} ({states})", since)
            raise ScenarioSetupError(f"party not {what} within {wait_s:g} s: {states}")
        self.waited(f"party still in combat ({states})", since, then="; finishing the fight")
        self.finish_fight(names)
        ok, bots = self._calm_wait(names, FIGHT_CALM_S, full_hp)
        if ok:
            self.waited(f"party {what} once the fight was finished ({', '.join(names)})", since)
            return
        raise ScenarioSetupError(f"party not {what} after finishing the fight: {self._states(names, bots)}")

    def fight_mobs(self, names: list[str], s: dict) -> list[tuple[float, dict]]:
        """The mobs fighting the party within fight_yd of a party member that is fighting, nearest first, as
        (yards from that member, unit). Only rows mob_ineligible passes: hostile creatures (0xF130) outside the party
        whose target is the leader or a party member - never a player, a party bot, a pet or a friendly NPC."""
        fighting = [b for n in names if (b := bot_of(s, n)) and in_combat(b)]
        if not fighting:
            return []
        party = self.party_guids()
        found = [(min(dist2d(u, b) for b in fighting), u) for u in self.units(near=150, limit=100)
                 if not mob_ineligible(u, party)]
        return sorted((t for t in found if t[0] <= self.cfg["fight_yd"]), key=lambda t: t[0])

    def _unit_row(self, mob: dict) -> dict | None:
        """The current `units` row of this guid (looked up by its entry), or None when it is not in view."""
        g = guid_int(mob.get("guid"))
        return next((u for u in self.units(entry=int(mob.get("entry") or 0), near=150, limit=100)
                     if guid_int(u.get("guid")) == g), None)

    def _mob_alive(self, mob: dict) -> bool:
        u = self._unit_row(mob)
        return bool(u) and not u.get("dead")

    def gm_die(self, mob: dict) -> None:
        """GM `.die` on this mob, the guard at the last step: GM on, the guid selected and confirmed, its fresh row
        still a mob fighting the party (mob_ineligible), and the selection still that guid right before the command.
        Anything else is refused with the reason (logged, kept in the evidence) and raised as a setup error."""
        name, guid = mob.get("name"), mob["guid"]

        def refuse(why: str):
            self.note(f"finish fight: GM .die on {name} {guid} refused: {why}")
            raise ScenarioSetupError(f"GM .die on {name} {guid} refused: {why}; the fight is not finished")

        if self.gm is not True:
            refuse("GM is off")
        if not self.select(guid):
            refuse("could not reselect it")
        why = mob_ineligible(self._unit_row(mob), self.party_guids())
        if why:
            refuse(why)
        if self.gm is not True:
            refuse("GM is off")
        if not self.selected(guid):
            refuse("the selection changed")
        self.cmd(".die")

    def _refused_since(self, seq: int) -> bool:
        """The server refused the order since seq: "is evading" (an evade-mode creature takes no hits) or TBM:ACTION_ERR."""
        return any("is evading" in (e.get("text") or "") or "TBM:ACTION_ERR" in (e.get("text") or "")
                   for e in self.events_since(seq, CHAT_KINDS))

    def finish_fight(self, names: list[str]) -> None:
        """Finish the fight the party is in, one mob at a time (FIGHT_MAX_MOBS at most). The mob is the nearest
        creature in combat within fight_yd of a fighting bot. It is selected and the selection confirmed, the party
        gets `.bot action attack` (it engages the selected target) and attack_wait_s to kill it, and a mob still
        alive then gets a GM `.die` on that same confirmed target (only with GM on). Nothing else is ever targeted."""
        for _ in range(FIGHT_MAX_MOBS):
            s = self.sample(Recording(self.now()), names)
            if not any(in_combat(bot_of(s, n)) for n in names):
                return
            mobs = self.fight_mobs(names, s)
            if not mobs:
                raise ScenarioSetupError("still in combat, and no creature in combat near the fighting bots to finish: "
                                         + self._states(names, [bot_of(s, n) for n in names]))
            d, mob = mobs[0]
            name, guid = mob.get("name"), mob["guid"]
            if not self.select(guid):
                raise ScenarioSetupError(f"could not select {name} {guid}: the fight is not finished (nothing else targeted)")
            why = mob_ineligible(self._unit_row(mob), self.party_guids())
            if why:  # it changed between the scan and the selection
                self.note(f"finish fight: no .bot action attack on {name} {guid}: {why}")
                raise ScenarioSetupError(f"{name} {guid} {why} once selected: the fight is not finished")
            if not self.selected(guid):
                raise ScenarioSetupError(f"the selection left {name} {guid}: the fight is not finished")
            seq, since = self.last_seq(), self.now()
            self.note(f"finish fight: {name} {guid} ({d:.0f} yd from the fighting bots) selected; .bot action attack")
            self.cmd(".bot action attack")
            refused = self._refused_since(seq)
            while not refused and self._mob_alive(mob) and self.now() - since < self.cfg["attack_wait_s"]:
                self._check_budget()
                self.wait(1.0)
                refused = self._refused_since(seq)
            if not self._mob_alive(mob):
                self.waited(f"{name} killed by .bot action attack", since)
            else:
                how = "refused (is evading)" if refused else "still alive"
                if self.gm is not True:
                    raise ScenarioSetupError(f"{name} {guid} {how} after .bot action attack, and GM is off so .die is"
                                             " not used: the fight is not finished")
                die_since = self.now()
                self.gm_die(mob)
                self.note(f"finish fight: GM .die on {name} {guid}, the same selected target ({how})")
                self.wait(1.0)
                if self._mob_alive(mob):
                    raise ScenarioSetupError(f"{name} {guid} still alive after GM .die")
                self.waited(f"{name} killed by GM .die", die_since)
            self._calm_wait(names, FIGHT_CALM_S)  # the dashboard can lag a few seconds behind the kill
        raise ScenarioSetupError(f"still in combat after {FIGHT_MAX_MOBS} mobs were finished")

    def wait_rested(self, names: list[str], wait_s: float, extend: bool = True) -> tuple[list[str], str]:
        """Wait until no bot is eating or drinking and has full HP and mana (a figure the sample lacks is skipped). The
        moon scenario pulls only a party that has nothing left to eat for. Returns the bots still busy and the rule that
        ended the wait: "rested" (none busy), "extended" (none busy, after the wait ran past wait_s on progress),
        "wait_s" (wait_s ran out with no busy bot making progress; extend=False ends it at wait_s whatever the progress),
        "hard cap" (REST_HARD_CAP_S ran out with one still making progress), "budget" (REST_MARGIN_S before the time
        budget ends: the caller reports the bots as not rested, it never lets _check_budget raise) or "no data" (the
        dashboard showed none of the party: at wait_s with no progress behind it, or REST_NO_DATA_SAMPLES samples in a
        row past wait_s). A busy bot made progress in the last REST_PROGRESS_S seconds when its HP or power (while its
        power type is unchanged) either reached a new high (a reading above its best since the wait began) or rose
        since the previous sample while the bot was eating or drinking. Regen without eating or drinking counts only
        as a new high; eating or drinking on its own, a buff's cost and a regen tick back to where it was do not count.
        A sample that shows none of the party (a failed dashboard read) never counts as rested; before wait_s it counts
        as busy, past wait_s it keeps the progress the last good sample showed."""
        t0 = self.now()
        end = t0 + wait_s
        cap = max(end, t0 + REST_HARD_CAP_S) if extend else end
        limit = self.deadline - REST_MARGIN_S
        rec = Recording(t0)
        best: dict[str, dict[str, float]] = {}  # bot -> its highest HP and power readings since the wait began
        last: dict[str, dict[str, float]] = {}  # bot -> its HP and power readings in the previous sample
        power_type: dict[str, str] = {}  # bot -> its power type at its last reading
        gain_at: dict[str, float] = {}  # bot -> the last time its HP or power reached a new high, or rose while eating
        extended, progress, failed_in_a_row = False, False, 0

        def note(n: str, b: dict, now: float) -> None:
            """A bot's HP and power readings: the first is the baseline, not a gain. A figure the sample lacks is
            skipped, not read as 0; a power type change starts the bot's power high and its last reading again."""
            marks, prev = best.setdefault(n, {}), last.setdefault(n, {})
            pt = str(b.get("power_type") or "")
            if power_type.get(n, pt) != pt:
                marks.pop("power", None)
                prev.pop("power", None)
            power_type[n] = pt
            eating = _eating(b)
            for fig in ("hp", "power"):
                v = b.get(fig)
                if v is None:
                    continue
                v = float(v)
                if fig not in marks:
                    marks[fig] = v
                elif v > marks[fig]:
                    marks[fig] = v
                    gain_at[n] = now
                if eating and v > prev.get(fig, math.inf):
                    gain_at[n] = now
                prev[fig] = v

        while True:
            s = self.sample(rec, names)
            now = self.now()
            failed = not any(bot_of(s, n) is not None for n in names)  # nothing of the party in it: not rested
            failed_in_a_row = failed_in_a_row + 1 if failed else 0
            busy = []
            if not failed:
                progress = False
            for n in names:
                b = bot_of(s, n)
                if b is None and not failed:
                    continue  # a good sample without this bot: judged by the bots it lists, as before
                if b is not None:
                    note(n, b, now)
                if failed or _eating(b) or _not_full(b):
                    busy.append(n)
                    progress = progress or (not failed and now - gain_at.get(n, -math.inf) <= REST_PROGRESS_S)
            if not busy:
                return [], ("extended" if extended else "rested")
            if now >= limit:
                return busy, "budget"
            if now >= end and (not extend or not progress or failed_in_a_row >= REST_NO_DATA_SAMPLES):
                return busy, ("no data" if failed else "wait_s")
            if now >= cap:
                return busy, "hard cap"
            extended = extended or now >= end
            self.wait(1.0)

    def require_rested(self, names: list[str], wait_s: float, when: str, extend: bool = True) -> None:
        """The party at full HP and mana with no one eating or drinking, or a setup error naming who is not. The wait's
        log line says which rule ended it (see wait_rested). extend=False: the wait ends at wait_s whatever the progress."""
        since = self.now()
        left, rule = self.wait_rested(names, wait_s, extend=extend)
        if not left:
            then = (f" (rule: all rested, past the {wait_s:g} s wait while a bot made progress)" if rule == "extended"
                    else " (rule: all rested)")
            self.waited(f"rested (full HP and mana, not eating or drinking) {when}: {', '.join(names)}", since, then=then)
            return
        who, head = ", ".join(left), f"party not rested (full HP and mana, not eating or drinking) {when}"
        if rule == "budget":
            self.waited(f"still not rested ({who}) {when}", since,
                        then=f" (rule: the time cap ran out {REST_MARGIN_S:g} s before its end)")
            raise ScenarioSetupError(f"{head}: {who} (the time cap ran out before it rested)")
        if rule == "hard cap":
            self.waited(f"still not rested ({who}) {when}", since,
                        then=f" (rule: the {REST_HARD_CAP_S:g} s hard cap ended a wait still making progress)")
            raise ScenarioSetupError(f"{head} within the {REST_HARD_CAP_S:g} s cap: {who} (a bot was still making progress)")
        if rule == "no data":
            self.waited(f"still not rested ({who}) {when}", since,
                        then=f" (rule: the dashboard gave no bot data at the end of the {wait_s:g} s wait, or {REST_NO_DATA_SAMPLES} reads in a row after it)")
            raise ScenarioSetupError(f"{head} within {wait_s:g} s: {who} (the dashboard gave no bot data)")
        if not extend:
            self.waited(f"still not rested ({who}) {when}", since,
                        then=f" (rule: the {wait_s:g} s wait ran out; this wait does not extend)")
            raise ScenarioSetupError(f"{head} within {wait_s:g} s: {who}")
        self.waited(f"still not rested ({who}) {when}", since,
                    then=f" (rule: the {wait_s:g} s wait ran out with no bot making progress)")
        raise ScenarioSetupError(f"{head} within {wait_s:g} s: {who} (no bot made progress in the last {REST_PROGRESS_S:g} s)")

    def ready(self, names: list[str], near_yd: float, rest_wait_s: float = REST_WAIT_S) -> None:
        """A scenario starts only when the party is ready: out of combat (a fight still on is finished), every bot on
        the leader's map within near_yd (summons re-sent), and rested (full HP and mana, not eating or drinking)."""
        self.wait_calm(names, self.cfg["calm_wait_s"])
        self.gather(names, near_yd)
        self.require_rested(names, rest_wait_s, "before the scenario starts")
        self.setup_ready(self._scenario_t0)

    def enter_dungeon(self, names: list[str]) -> None:
        if self.status().get("map") != self.cfg["dungeon"]["map"]:
            self.gm_on()
            self.goto(self.cfg["dungeon"]["enter"])
        else:  # a fight left over from the last scenario: `.bot summon` is refused in combat
            self.wait_calm(names, self.cfg["calm_wait_s"])
        self.gather(names)

    def place(self, spot: dict, names: list[str], near_yd: float = 20.0, gm_off: bool = False,
              rest_wait_s: float = REST_WAIT_S) -> None:
        """In the dungeon at a spot, ready (see ready). gm_off (a scenario's "gm_off": true) turns GM off the safe
        way (safe spot first) before going to the spot."""
        self.gm_on()
        self.enter_dungeon(names)
        if gm_off:
            self.gm_off()
        self.goto(spot)
        self.wait(2.0)
        self.ready(names, near_yd, rest_wait_s)

    def respawn_near(self) -> None:
        """`.respawn` with nothing selected respawns every dead creature within visibility range. A selected
        live creature would make it a no-op, so select a guid that does not exist first."""
        self.call("target", guid="0xF1300000000FFFFF")
        self.cmd(".respawn")
        self.wait(2.0)

    @staticmethod
    def unit_alive(u: dict) -> bool:
        """A units row that is alive: no `dead` flag and (when the row carries it) HP above 0."""
        hp = u.get("health")
        return not u.get("dead") and not (isinstance(hp, (int, float)) and hp <= 0)

    def find_pack(self, at: dict, entries: list[int], within_yd: float = 12.0) -> list[dict | None]:
        """For each entry, the nearest living creature of that entry within within_yd of `at` (distinct)."""
        taken, out = set(), []
        for e in entries:
            us = [u for u in self.units(entry=int(e), near=150, limit=100)
                  if self.unit_alive(u) and u["guid"] not in taken and dist2d(u, at) <= within_yd]
            u = min(us, key=lambda u: dist2d(u, at)) if us else None
            if u:
                taken.add(u["guid"])
            out.append(u)
        return out

    def find_pack_confirmed(self, at: dict, entries: list[int]) -> list[dict | None]:
        """find_pack, looked at twice PACK_CONFIRM_S apart: a creature the last scenario killed a moment ago can still be
        listed alive (live: ranged_reach pulled a pack the stay scenario had just killed), so the pack counts only
        when the second look still finds it, and the second look is what is returned."""
        first = self.find_pack(at, entries)
        if any(u is None for u in first):
            return first
        self.wait(PACK_CONFIRM_S)
        second = self.find_pack(at, entries)
        gone = [str(a["guid"]) for a, b in zip(first, second) if b is None]
        if gone:
            self.note(f"pack member(s) {', '.join(gone)} listed alive, then dead {PACK_CONFIRM_S:g} s later")
        return second

    def wait_pack(self, at: dict, entries: list[int]) -> list[dict | None]:
        """After `.respawn`: poll until every pack member is listed alive and out of combat in two reads in a row
        (the respawn takes a moment to show, and the first read can still carry the old state), at most
        PACK_RESPAWN_WAIT_S and never past the scenario budget. Returns the last read."""
        since, end = self.now(), self.now() + PACK_RESPAWN_WAIT_S
        prev, pack = None, self.find_pack(at, entries)
        while True:
            ok = all(u is not None and not u.get("in_combat") for u in pack)
            if ok and prev is not None and [u["guid"] for u in prev] == [u["guid"] for u in pack]:
                break
            if self.now() >= end:
                break
            self._check_budget()
            self.wait(1.0)
            prev, pack = (pack if ok else None), self.find_pack(at, entries)
        self.waited(f"pack {entries} alive after .respawn", since)
        return pack

    def prepare_pack(self, at: dict, entries: list[int]) -> list[dict]:
        """The dungeon's own pack at `at`, alive and out of combat; `.respawn` once when it is not (dead, in combat, or
        listed alive only because the list lags a kill), then wait until the respawned units are listed alive."""
        pack = self.find_pack_confirmed(at, entries)
        if any(u is None or u.get("in_combat") for u in pack):
            self.respawn_near()
            pack = self.wait_pack(at, entries)
        if any(u is None for u in pack):
            raise ScenarioSetupError(f"no live pack {entries} within 12 yd of {spot_text(at)} after .respawn"
                                     f" (waited {PACK_RESPAWN_WAIT_S:g} s for it to be listed alive)")
        if any(u.get("in_combat") for u in pack):
            raise ScenarioSetupError(f"the pack at {spot_text(at)} is already in combat")
        return pack

    # -- scenarios
    def sc_follow_into_dungeon(self, sc: dict) -> Outcome:
        roles = self.ensure_party()
        names = list(roles.values())
        map_id = self.cfg["dungeon"]["map"]
        self.gm_on()
        self.goto(self.cfg["start"])
        self.wait(2.0)
        self.ready(names, 40, sc.get("rest_wait_s", REST_WAIT_S))
        rec = Recording(self.now())
        pre = self.sample(rec, names)
        notes = [f"before: {n} strategies {'hold' if 'follow' in strategies(bot_of(pre, n)) else 'LACK'} follow"
                 for n in names]
        t_tele = round(self.now() - rec.t0, 2)
        self.goto(self.cfg["dungeon"]["enter"])

        def done(r):
            return evaluate_follow_into_dungeon(r.samples, names, t_tele, map_id, sc["within_s"]).verdict == PASS

        self.observe(rec, names, max(sc["within_s"] + 2 - (self.now() - rec.t0 - t_tele), 1), until=done)
        out = evaluate_follow_into_dungeon(rec.samples, names, t_tele, map_id, sc["within_s"])
        if out.verdict == INCONCLUSIVE:  # bots fighting on arrival: wait for an out-of-combat strategy list
            self.observe(rec, names, sc["extra_s"],
                         until=lambda r: evaluate_follow_into_dungeon(r.samples, names, t_tele, map_id, sc["within_s"]).verdict != INCONCLUSIVE)
            out = evaluate_follow_into_dungeon(rec.samples, names, t_tele, map_id, sc["within_s"])
        elif out.verdict == FAIL and len(out.metrics["arrival_s"]) < len(names):  # how late are the others?
            self.observe(rec, names, sc["extra_s"],
                         until=lambda r: all(any((b := bot_of(s, n)) and b.get("map") == map_id for s in r.samples[-1:])
                                             for n in names))
            out = evaluate_follow_into_dungeon(rec.samples, names, t_tele, map_id, sc["within_s"])
        out.evidence += notes
        return out

    def sc_stay_then_order_follows(self, sc: dict) -> Outcome:
        roles = self.ensure_party()
        names, bot = list(roles.values()), roles[sc["bot"]]
        self.place(sc["spot"], names, gm_off=sc.get("gm_off", False), rest_wait_s=sc.get("rest_wait_s", REST_WAIT_S))
        rec = Recording(self.now())
        try:
            seq_stay, t_stay = self.last_seq(), self.now()
            self.whisper(bot, "stay")
            self.observe(rec, names, 4)
            st = strategies(bot_of(rec.samples[-1], bot))
            said = self.whispers_from(seq_stay, bot, t_stay + STAY_REPLY_WAIT_S)
            notes = [f"after `stay`: {bot} replied {said[-1].split('whispers: ', 1)[-1]!r}" if said else
                     f"after `stay`: no whisper reply from {bot}",
                     f"dashboard 4 s after `stay`: {'stay' if 'stay' in st else 'no stay'},"
                     f" {'follow' if 'follow' in st else 'no follow'} (the list can lag a few seconds)"]
            # a mob next to the bot: a bot on `stay` holds its spot, so a far target is never reached (live:
            # 25 s+ "combat" at 39 yd). Summoned with GM on it ignores the player and takes the bots.
            mob = (self.prepare_pack(sc["mob_at"], [int(sc["mob_entry"])])[0] if sc.get("mob_at")
                   else self.summon(int(sc["mob_entry"])))
            if not self.select(mob["guid"]):
                raise ScenarioSetupError(f"could not select {mob.get('name')}")
            notes.append(f"attack target: {mob.get('name')} {mob['guid']} at {_xy(mob)}")
            if sc.get("mob_hp"):
                self.cmd(f".modify hp {int(sc['mob_hp'])}")
            seq = self.last_seq()
            t_order = round(self.now() - rec.t0, 2)
            self.cmd(f".bot attack {bot}")

            def mob_dead(_r):
                us = [u for u in self.units(entry=int(sc["mob_entry"]), near=150, limit=100) if u["guid"] == mob["guid"]]
                return not us or us[0].get("dead")

            self.observe(rec, names, sc["fight_max_s"],
                         until=lambda r: mob_dead(r) and not in_combat(bot_of(r.samples[-1], bot)))
            evs = self.events_since(seq, COMBAT_KINDS)
            fought = any((e.get("source") or "").lower() == bot.lower() and str(e.get("target_guid") or "").lower()
                         == str(mob["guid"]).lower() for e in evs) or None
            self.observe(rec, names, 3)
            t_walk_start = round(self.now() - rec.t0, 2)
            self.walk(rec, names, [sc["walk_to"]])
            t_walk_end = round(self.now() - rec.t0, 2)

            def close(r):
                d = [leader_dist(s, bot) for s in r.samples[-2:]]
                return len(d) == 2 and all(x is not None and x <= sc["follow_max_yd"] for x in d)

            self.observe(rec, names, sc["settle_s"], until=close)
            out = evaluate_stay_then_follow(rec.samples, bot, t_order, t_walk_start, t_walk_end,
                                            sc["follow_max_yd"], sc["min_move_yd"], fought)
            out.evidence += notes + [f"reply: {l}" for l in self.replies(seq, [bot])]
            return out
        finally:
            self.whisper(bot, "follow")

    def sc_healer_follows_ooc(self, sc: dict) -> Outcome:
        roles = self.ensure_party()
        names, healer = list(roles.values()), roles[sc["healer"]]
        self.place(sc["start_spot"], names, near_yd=15, gm_off=sc.get("gm_off", False),
                   rest_wait_s=sc.get("rest_wait_s", REST_WAIT_S))
        self.wait_calm(names, sc["ooc_wait_s"], full_hp=True)
        rec = Recording(self.now())
        self.sample(rec, names)
        self.walk(rec, names, [sc["end_spot"]])
        t_walk_end = round(self.now() - rec.t0, 2)

        def close(r):
            if r.samples[-1]["t"] - t_walk_end < 3:
                return False
            d = [leader_dist(s, healer) for s in r.samples[-2:]]
            return all(x is not None and x <= sc["end_max_yd"] for x in d)

        self.observe(rec, names, sc["settle_s"], until=close)
        return evaluate_healer_follows(rec.samples, healer, t_walk_end, sc["end_max_yd"], sc["stall_s"])

    def sc_ranged_reach_in_dungeon(self, sc: dict) -> Outcome:
        roles = self.ensure_party()
        names = list(roles.values())
        casters = [roles[k] for k in sc["casters"]]
        self.place(sc["spot"], names, gm_off=sc.get("gm_off", False), rest_wait_s=sc.get("rest_wait_s", REST_WAIT_S))
        mobs = self.prepare_pack(sc["pull_at"], sc["pull_entries"])
        if not self.select(mobs[0]["guid"]):
            raise ScenarioSetupError("could not select the pull target")
        rec = Recording(self.now())
        wall0 = self._wall()  # the samples' clock starts here: the agent's event times (epoch) are shifted onto it
        seq = self.last_seq()
        self.cmd(sc["order"].format(**roles))
        state = {"fought": False, "calm_since": None}

        def settled(r):
            if not state["fought"] and self.order_refused(seq):
                return True
            s = r.samples[-1]
            fighting = any(in_combat(b) for b in s["bots"].values())
            state["fought"] = state["fought"] or fighting
            if fighting or not state["fought"]:
                state["calm_since"] = None
                return False
            state["calm_since"] = state["calm_since"] if state["calm_since"] is not None else s["t"]
            return s["t"] - state["calm_since"] >= sc["quiet_s"]

        self.observe(rec, names, sc["fight_max_s"], until=settled)
        recs = align(records_from_events(self.events_since(seq, COMBAT_KINDS), names), wall0)
        out = evaluate_ranged_reach(rec.samples, casters, sc["standstill_s"], records=recs)
        out.metrics["samples"] = len(rec.samples)
        out.evidence += [f"reply: {l}" for l in self.replies(seq, names)]
        return out

    def sc_moon_sheep_lands(self, sc: dict) -> Outcome:
        roles = self.ensure_party()
        names, mage = list(roles.values()), roles[sc["mage"]]
        self.place(sc["spot"], names, gm_off=sc.get("gm_off", False), rest_wait_s=sc.get("rest_wait_s", REST_WAIT_S))
        pack = self.prepare_pack(sc["pack_at"], sc["pack_entries"])
        moon, pull = pack[sc["moon_index"]], pack[sc["pull_index"]]
        self.require_rested(names, sc.get("rest_wait_s", 45), "before the cc order")
        seq0 = self.last_seq()
        try:
            if not self.select(moon["guid"]):
                raise ScenarioSetupError("could not select the Moon mob")
            seq_cc = self.order_mark("moon", mage, moon)
            acks = self.acks_from(seq_cc, self.now() + ACK_REPLY_WAIT_S)  # polled: the ACK can arrive late
            if not self.select(pull["guid"]):
                raise ScenarioSetupError("could not select the pull mob")
            self.require_rested(names, sc.get("rest_recheck_s", 15), "right before the pull", extend=False)
            rec = Recording(self.now())
            rec.extra["moon_combat"] = False
            t_pull = self._wall()
            seq_pull = self.last_seq()
            self.cmd(sc["order"].format(**roles))
            state = {"fought": False, "calm_since": None}

            def moon_flag(_s):
                us = [u for u in self.units(entry=int(moon.get("entry") or 0), near=120, limit=50)
                      if str(u["guid"]).lower() == str(moon["guid"]).lower()]
                if us and (us[0].get("in_combat") or us[0].get("dead")):
                    rec.extra["moon_combat"] = True
                    rec.extra.setdefault("moon_combat_t", self._wall())

            def settled(r):
                if not state["fought"] and self.order_refused(seq_pull):
                    return True
                s = r.samples[-1]
                fighting = any(in_combat(b) for b in s["bots"].values())
                state["fought"] = state["fought"] or fighting
                if fighting or not state["fought"] or s["t"] < sc["within_s"] + 2:
                    state["calm_since"] = None
                    return False
                state["calm_since"] = state["calm_since"] if state["calm_since"] is not None else s["t"]
                return s["t"] - state["calm_since"] >= sc["quiet_s"]

            self.observe(rec, names, sc["watch_s"], until=settled, each=moon_flag)
            recs = records_from_events(self.events_since(seq0, COMBAT_KINDS), names + [self.me])
            mage_guid = next((b.get("guid") for s in reversed(rec.samples) if (b := bot_of(s, mage))), None)
            out = evaluate_moon_sheep(recs, mage, {"guid": moon["guid"], "name": moon.get("name")}, t_pull,
                                      sc["within_s"], rec.extra["moon_combat"], mage_guid,
                                      rec.extra.get("moon_combat_t"))
            out.evidence.append(f"Moon mob {moon.get('name')} {moon['guid']} at {_xy(moon)}; pull target"
                                f" {pull.get('name')} {pull['guid']} at {_xy(pull)}")
            out.evidence.append(f"cc order reply: {acks[-1] if acks else '(no TBM:ACTION line seen)'}")
            out.evidence += [f"reply: {l}" for l in self.replies(seq0, names)]
            return out
        finally:
            self.cmd(f".bot action cc clear {mage}")

    def sc_no_jumps_in_dungeon(self, sc: dict) -> Outcome:
        roles = self.ensure_party()
        names = list(roles.values())
        route = sc["route"]
        self.place(route[0], names, gm_off=sc.get("gm_off", False), rest_wait_s=sc.get("rest_wait_s", REST_WAIT_S))
        rec = Recording(self.now())
        self.walk(rec, names, route[1:])
        self.observe(rec, names, 3)
        out = evaluate_no_jumps(self.all_samples, self.cfg["dungeon"]["map"], sc["min_samples"])
        out.evidence.append(f"read all {len(self.all_samples)} dashboard samples of this gate run"
                            f" ({len(rec.samples)} from this scenario's own walk)")
        return out

    # -- cleanup and the run
    def scenario_cleanup(self) -> list[str]:
        notes = []
        for step in (lambda: self.call("stop_attack"), lambda: notes.extend(self.kill_leftovers()), self.gm_on):
            try:
                step()
            except Exception as err:  # cleanup must go on
                notes.append(f"cleanup: {type(err).__name__}: {err}")
        return notes

    def teardown(self, logout: bool = True) -> dict:
        """Before logout: stop_attack, leave_group, `.gm on`; a refused logout gets `.gm on`, a wait and
        one retry. Without logout: stop_attack, `.gm on`, safe spot, party kept."""
        steps, result = [], "kept in world"

        def step(name, fn):
            try:
                fn()
                steps.append(f"{name}: ok")
                return True
            except Exception as err:
                steps.append(f"{name}: {err}")
                return False

        step("stop_attack", lambda: self.call("stop_attack"))
        step("kill leftovers", lambda: steps.extend(self.kill_leftovers()))
        if not logout:
            step(".gm on", self.gm_on)
            step("safe spot", lambda: self.goto(self.cfg["safe"]))
            return {"steps": steps, "result": result}
        step("leave_group", lambda: self.call("leave_group") if self.party_names() else None)
        step(".gm on", self.gm_on)
        old = getattr(self.c, "timeout_s", None)
        if old is not None:
            self.c.timeout_s = max(old, 45.0)
        try:
            if step("logout", lambda: self.call("logout")):
                result = "logged out"
            elif any(w in steps[-1] for w in ("disconnected by the server", "session closed")):
                # live b13: the server closed the socket instead of sending SMSG_LOGOUT_COMPLETE; the session is
                # over, so a retry can only fail on a closed agent
                result = "session closed by the server during logout (no SMSG_LOGOUT_COMPLETE)"
            else:
                step(".gm on (retry)", self.gm_on)
                self.wait(self.cfg["logout_retry_wait_s"])
                result = "logged out on retry" if step("logout retry", lambda: self.call("logout")) else "LOGOUT REFUSED"
        finally:
            if old is not None:
                self.c.timeout_s = old
        return {"steps": steps, "result": result}

    def run(self, names: list[str], logout: bool = True, label: str = "") -> dict:
        started = self._wall()
        gate_end = self.now() + self.cfg["budget_s"]
        results, teardown, abort = [], None, None
        try:
            st = self.status()
            self.me = st.get("character") or ""
            if st.get("state") != "in_world":
                abort = f"turtle-agent is {st.get('state')!r}, not in_world"
        except HeadlessError as err:
            abort = str(err)
        try:
            for name in names:
                sc = self.cfg["scenarios"][name]
                t0 = self.now()
                if abort:
                    out = Outcome(INCONCLUSIVE, [f"not run: {abort}"])
                elif gate_end - t0 < sc["budget_s"] * 0.5:
                    out = Outcome(INCONCLUSIVE, ["not run: the gate's time budget is used up"])
                else:  # setup has its own cap; the scenario's budget starts once the party is ready (setup_ready)
                    self.gate_end, self.scenario_budget, self._scenario_t0 = gate_end, sc["budget_s"], t0
                    self.deadline = min(t0 + self.cfg["setup_budget_s"], gate_end)
                    self.in_setup, self.setup_s = True, None
                    self.current = name
                    self.log(f"== {name}")
                    self.setup_log = []
                    out = self._run_one(name, sc)
                    self.deadline, self.in_setup = float("inf"), False
                    out.evidence += self.setup_log
                    out.evidence += self.scenario_cleanup()
                    if any(e.startswith("turtle-agent:") for e in out.evidence) and self._agent_gone():
                        abort = "turtle-agent stopped answering"
                self.deadline, self.in_setup = float("inf"), False
                results.append({"name": name, "verdict": out.verdict, "seconds": round(self.now() - t0, 1),
                                "setup_s": self.setup_s if not abort else None,
                                "evidence": out.evidence, "metrics": out.metrics})
                self.setup_s = None
                self.log(f"   {name}: {out.verdict.upper()}")
        finally:
            if not abort:
                teardown = self.teardown(logout)
        verdicts = [r["verdict"] for r in results]
        overall = FAIL if FAIL in verdicts else INCONCLUSIVE if INCONCLUSIVE in verdicts or not verdicts else PASS
        return {"gate": "tortoisebots-buildgate", "label": label, "character": self.me,
                "started": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
                "seconds": round(self._wall() - started, 1), "verdict": overall, "scenarios": results,
                "teardown": teardown or {"steps": [f"skipped: {abort}"], "result": "not run"},
                "commands": self.actions}

    def _run_one(self, name: str, sc: dict) -> Outcome:
        try:
            return getattr(self, "sc_" + name)(sc)
        except BudgetExceeded as err:
            return Outcome(INCONCLUSIVE, [f"{err}"])
        except ScenarioSetupError as err:
            return Outcome(INCONCLUSIVE, [f"setup: {err}"])
        except HeadlessError as err:
            return Outcome(INCONCLUSIVE, [f"turtle-agent: {err}"])
        except DashboardUnavailable as err:
            return Outcome(INCONCLUSIVE, [f"dashboard: {err}"])
        except Exception as err:  # a gate bug must not skip the other scenarios or the teardown
            return Outcome(INCONCLUSIVE, [f"error: {type(err).__name__}: {err}"])

    def _agent_gone(self) -> bool:
        try:
            self.status()
            return False
        except HeadlessError:
            return True


# ---------------------------------------------------------------- plan and report

def plan(cfg: dict, names: list[str], logout: bool = True) -> list[str]:
    """The dry-run plan: what the gate would do, with thresholds, in order. No network."""
    dg, scs = cfg["dungeon"], cfg["scenarios"]
    party = ", ".join(f"{p['key']}={p['hire']}" for p in cfg["party"])
    out = [f"Build gate plan: {len(names)} scenario(s), gate budget {cfg['budget_s']} s, dungeon map {dg['map']}"
           f" (enter {spot_text(dg['enter'])}), safe spot {spot_text(cfg['safe'])}",
           f"party: {party} (reused when the party already has these classes; else `.bot hire`"
           + (f" at {spot_text(cfg['hire_at'])})" if cfg.get("hire_at") else ")"),
           "safety: every `.gm off` = `.gm on` + teleport to the safe spot first; scenarios run with GM on;"
           " fights use the dungeon's own packs (.respawn when dead)",
           f"setup before each scenario: out of combat (up to {cfg['calm_wait_s']:g} s; a fight still on is finished:"
           " `.bot action attack` on a hostile creature (0xF130, not in the party, not a pet, player or NPC) whose"
           " target is the leader or a party member, else GM `.die` on that same re-checked target, GM on only);"
           " every bot on the leader's map within range (summons re-sent, 3 at most); rested (full HP and mana)."
           f" Setup is capped at {cfg['setup_budget_s']:g} s; a scenario's budget starts once the party is ready"]
    total = scen = 0
    for n in names:
        s = scs[n]
        scen += s["budget_s"]
        total += s["budget_s"] + cfg["setup_budget_s"]
        out.append(f"\n[{n}] setup <= {cfg['setup_budget_s']:g} s, then budget {s['budget_s']} s")
        if n == "follow_into_dungeon":
            out += [f"  .gm on; go to start {spot_text(cfg['start'])}; `.bot summon` bots further than 40 yd",
                    f"  {spot_text(dg['enter'])}; sample the dashboard every {cfg['sample_s']:g} s",
                    f"  PASS: all {len(cfg['party'])} bots on map {dg['map']} within {s['within_s']} s and their"
                    " out-of-combat strategy lists hold `follow`"]
        elif n == "stay_then_order_follows":
            out += [f"  place the party at {spot_text(s['spot'])}; /w <{s['bot']}> stay",
                    (f"  select the live {s['mob_entry']} at {spot_text(s['mob_at'])} (.respawn if dead)" if s.get("mob_at")
                     else f"  .npc summon {s['mob_entry']} at the player (GM on: it takes the bots), select it")
                    + (f", .modify hp {s['mob_hp']}" if s.get("mob_hp") else "") + f"; .bot attack <{s['bot']}>",
                    f"  after the fight walk to {spot_text(s['walk_to'])}; watch {s['settle_s']} s",
                    f"  PASS: strategies hold `follow` after the fight and the bot ends <= {s['follow_max_yd']} yd from"
                    f" the leader having moved >= {s['min_move_yd']} yd; then /w <{s['bot']}> follow (restore)"]
        elif n == "healer_follows_ooc":
            out += [f"  place the party at {spot_text(s['start_spot'])}; wait until out of combat at full HP"
                    f" (<= {s['ooc_wait_s']} s)",
                    f"  walk to {spot_text(s['end_spot'])} in legs of <= {cfg['leg_yd']:g} yd; watch <= {s['settle_s']} s",
                    f"  PASS: healer ends <= {s['end_max_yd']} yd; FAIL also on a standstill >= {s['stall_s']} s"
                    f" with {REACH_HEAL!r}"]
        elif n == "ranged_reach_in_dungeon":
            out += [f"  place the party at {spot_text(s['spot'])}; the live pack {s['pull_entries']} at"
                    f" {spot_text(s['pull_at'])} (.respawn if dead)",
                    f"  select the first mob; {s['order']}; sample every {cfg['sample_s']:g} s until"
                    f" {s['quiet_s']} s calm or {s['fight_max_s']} s",
                    f"  FAIL: {', '.join(s['casters'])} standing still >= {s['standstill_s']} s with"
                    f" {REACH_SPELL!r} or {REACH_HEAL!r} and no cast in that stretch"]
        elif n == "moon_sheep_lands":
            out += [f"  place the party at {spot_text(s['spot'])}; the live pack {s['pack_entries']} at {spot_text(s['pack_at'])}"
                    " (.respawn if dead)",
                    f"  wait until the party is at full HP and mana, not eating or drinking (<= {s.get('rest_wait_s', 45)} s)",
                    f"  select pack[{s['moon_index']}]; .bot action cc moon <{s['mage']}>; select pack[{s['pull_index']}];"
                    f" re-check rested (<= {s.get('rest_recheck_s', 15)} s); {s['order']}; watch <= {s['watch_s']} s",
                    f"  PASS: first Polymorph on the Moon mob completes <= {s['within_s']} s after the Moon mob enters combat"
                    " (never in combat: INCONCLUSIVE) and no party damage lands on it before the sheep; report starts"
                    " vs completions and the pull-anchored time; then cc clear"]
        elif n == "no_jumps_in_dungeon":
            out += [f"  walk the route {' -> '.join(spot_text(p) for p in s['route'])}",
                    f"  FAIL: any bot last_action containing 'jump' on map {dg['map']} in any sample of the run"
                    f" (needs >= {s['min_samples']} samples)"]
    out.append(f"\nteardown: stop_attack, kill leftovers, " + (
        "leave_group, .gm on, logout (refused: .gm on, wait, retry once, report)" if logout
        else ".gm on, safe spot, keep the party (--no-logout)"))
    out.append(f"worst case {total:g} s of {cfg['budget_s']} s gate budget ({scen} s of scenario budgets +"
               f" {len(names)} x {cfg['setup_budget_s']:g} s setup)"
               + ("; the gate budget ends the run first: a scenario that would start with less than half its budget"
                  " left is reported not run" if total > cfg["budget_s"] else ""))
    return out


def render_markdown(rep: dict) -> str:
    out = [f"# Build gate {rep.get('label') or ''}".rstrip(), "",
           f"- verdict: **{rep['verdict'].upper()}**",
           f"- started {rep['started']}, {rep['seconds']} s, character {rep.get('character') or '?'}",
           f"- teardown: {rep['teardown']['result']}", "",
           "| scenario | verdict | s | first evidence |", "|---|---|---|---|"]
    for r in rep["scenarios"]:
        first = (r["evidence"][0] if r["evidence"] else "").replace("|", "/")
        secs = f"{r['seconds']} (setup {r['setup_s']})" if r.get("setup_s") is not None else f"{r['seconds']}"
        out.append(f"| {r['name']} | {r['verdict'].upper()} | {secs} | {first} |")
    for r in rep["scenarios"]:
        out += ["", f"## {r['name']}: {r['verdict'].upper()}", ""]
        out += [f"- {e}" for e in r["evidence"]] or ["- (no evidence)"]
        if r.get("metrics"):
            out.append(f"- metrics: `{json.dumps(r['metrics'], sort_keys=True)}`")
    out += ["", "## Teardown", ""] + [f"- {s}" for s in rep["teardown"]["steps"]]
    return "\n".join(out) + "\n"

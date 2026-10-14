"""Fight timeline: turtle-agent's combat and chat events merged with the bot dashboard's per-bot samples into
one row per second, with the annotations that make a TortoiseBots fix measurable:

- standstill out of range: a reach action ("reach spell", "reach party member to heal") held for standstill_s
  (5 s) or more without moving or casting, while the target is out of range (spell 30 yd, heal 40 yd; when the
  target's position is unknown the reach action is taken as the out-of-range signal, as the build gate does);
- CC (Polymorph) cast starts vs completions per caster, with the first completion after the pull;
- first-heal latency after a party member (bot or the player) drops below 70 % health;
- deaths of party members, each with its last 5 s (combat records and dashboard samples).

Inputs, on one clock (seconds; align() shifts epoch records onto the samples' clock):
- samples: {"t", "leader": {"name","map","x","y","health","max_health"} | None, "bots": {lowercase name:
  dashboard bot dict}, "units": [{"name","x","y","dead"}, ...]} - the build gate's sample plus the units;
- records: buildgate.records_from_events / records_from_lines plus heals, deaths, kills and chat
  (records_from_agent_events / records_from_log_lines).

The scoring helpers (standstill_runs, leader_dist, the record builders) are the build gate's; this module
adds the per-second view and the extra annotations. record_fight() records a live window through
turtle-agent and the dashboard (the MCP tool fight_timeline); everything else is pure.
"""
import json
import math
import os
import re
import time

from . import buildgate as bg
from .combat import parse_lines
from .dashboard import DashboardUnavailable

REACH_ACTIONS = (bg.REACH_SPELL, bg.REACH_HEAL)
OPTS = {"spell_range_yd": 30.0, "heal_range_yd": 40.0, "standstill_s": 5.0, "still_yd": 1.0, "move_yd": 0.5,
        "low_pct": 70, "death_window_s": 5.0, "max_cast_s": 3.0, "max_rows": 900, "move_window_s": 2.0}
METRIC_KEYS = ("standstill_s", "first_heal_s", "unhealed_drops", "sheep_s", "sheep_wasted_casts", "follow_yd", "deaths")
GAINS_HEALTH = re.compile(r"^(.+?) gains (\d+) health from (?:your() (.+?)|(.+?)'s (.+?))\.$")
CHAT_KINDS = ("chat", "whisper", "system")


# ---------------------------------------------------------------- records

def records_from_agent_events(evs: list[dict], party_names=(), poly_ids=bg.POLYMORPH_IDS) -> list[dict]:
    """turtle-agent events as records (t = epoch seconds): the build gate's damage / start / go / aura records
    plus heal, death, kill, CC cast failures and chat lines."""
    party = {n.lower() for n in party_names}
    out = bg.records_from_events(evs, party_names, poly_ids)
    for e in evs:
        k, d = e.get("kind"), e.get("data") or {}
        if not e.get("time"):
            continue
        kind = {"heal": "heal", "death": "death", "kill": "kill", "spell_fail": "fail"}.get(k)
        if k == "periodic" and d.get("tick") == "heal":
            kind = "heal"
        if k in CHAT_KINDS:
            kind = "chat"
        if kind is None or (kind == "fail" and e.get("spell_id") not in poly_ids):
            continue
        src = e.get("source") or ""
        out.append({"t": bg.iso_epoch(e["time"]), "kind": kind, "source": src, "source_guid": e.get("source_guid"),
                    "target": e.get("target") or "", "target_guid": e.get("target_guid"),
                    "poly": e.get("spell_id") in poly_ids, "amount": e.get("amount") or 0, "spell_id": e.get("spell_id"),
                    "party": bool(e.get("party")) or src.lower() in party, "line": None, "text": e.get("text"),
                    "cast_ok": k == "heal"})
    return sorted(out, key=lambda r: r["t"])


def records_from_log_lines(lines: list[str], me: str, party_names=()) -> list[dict]:
    """1.12 combat-log lines as records (t = seconds since midnight): the build gate's records plus heals
    (also "X gains N health from Y's Z."), deaths and kills."""
    party = {n.lower() for n in party_names} | {me.lower()}
    out = bg.records_from_lines(lines, me, party_names)
    for e in parse_lines(lines, me):
        if not e.time:
            continue
        rec = None
        if e.kind == "heal":
            rec = ("heal", e.source, e.target, e.amount)
        elif e.kind == "dies":
            rec = ("death", e.source, "", 0)
        elif e.kind == "slain":
            rec = ("kill", e.source, e.target, 0)
        elif e.kind == "gain" and (m := GAINS_HEALTH.match(e.raw.split("  ", 1)[-1])):
            rec = ("heal", me if m.group(3) is not None else m.group(5), m.group(1), int(m.group(2)))
        if rec:
            kind, src, tgt, amt = rec
            out.append({"t": bg.clock_seconds(e.time), "kind": kind, "source": src or "", "source_guid": None,
                        "target": tgt or "", "target_guid": None, "poly": False, "amount": amt or 0,
                        "party": (src or "").lower() in party, "line": e.raw})
    return sorted(out, key=lambda r: r["t"])


align = bg.align  # records shifted onto the samples' clock (the build gate shifts its records the same way)


# ---------------------------------------------------------------- per-sample helpers

def _pct(cur, mx):
    return round(100 * (cur or 0) / mx) if mx else None


def _mana(b: dict):
    if (b.get("power_type") or "mana") != "mana":
        return None
    return _pct(b.get("power"), b.get("max_power"))


def target_dist(s: dict, b: dict):
    """2D distance from a bot to its target (by name): another party bot, the player or a recorded unit,
    the nearest living match on the bot's map. None when the target's position is unknown."""
    name = str(b.get("target") or "").lower()
    if not name:
        return None
    if name == str(b.get("name") or "").lower():
        return 0.0
    cands = [o for o in (s.get("bots") or {}).values() if str(o.get("name") or "").lower() == name]
    lead = s.get("leader") or {}
    if str(lead.get("name") or "").lower() == name:
        cands.append(lead)
    cands += [u for u in s.get("units") or [] if str(u.get("name") or "").lower() == name and not u.get("dead")]
    cands = [c for c in cands if c.get("map") in (None, b.get("map"))]
    return round(min(bg.dist2d(b, c) for c in cands), 1) if cands else None


def _member_pct(s: dict, name: str):
    b = bg.bot_of(s, name)
    if b is not None:
        return _pct(b.get("hp"), b.get("max_hp"))
    lead = s.get("leader") or {}
    if str(lead.get("name") or "").lower() == name.lower():
        return _pct(lead.get("health"), lead.get("max_health"))
    return None


def _dead(s: dict, name: str) -> bool:
    b = bg.bot_of(s, name)
    if b is None:
        return False
    return b.get("state") == "dead" or (b.get("max_hp") and b.get("hp") == 0)


def _short(r: dict, me: str) -> str:
    """One record from one side: "damage Oracle 225" (caused) / "damage from Oracle 40" (taken)."""
    spell = r.get("spell") or ("Polymorph" if r.get("poly") else "")
    kind = {"start": "cast start", "go": "cast", "aura": "aura", "fail": "cast failed"}.get(r["kind"], r["kind"])
    head = f"{kind} {spell}".strip()
    if me == "caused":
        other = r.get("target") or ""
        return f"{head} {other} {r['amount'] or ''}".strip() if other else head
    src = f" from {r['source']}" if r.get("source") else ""
    return f"{head}{src} {r['amount'] or ''}".strip()


# ---------------------------------------------------------------- annotations

def _standstills(samples: list[dict], name: str, o: dict, records=()) -> list[dict]:
    """bg.standstill_runs over a view where a sample whose target is known to be in range does not count
    as a reach sample (it breaks the run), and cut at the bot's casts (records on the samples' clock)."""
    view = []
    for s in samples:
        b = bg.bot_of(s, name)
        if b is not None:
            act = str(b.get("last_action") or "").lower()
            rng = o["heal_range_yd"] if bg.REACH_HEAL in act else o["spell_range_yd"]
            d = target_dist(s, b)
            if d is not None and d <= rng:
                s = {**s, "bots": {**s["bots"], name.lower(): {**b, "last_action": "(reach, in range)"}}}
        view.append(s)
    out = []
    casts = bg.cast_times(records, name)
    for r in bg.standstill_runs(view, name, REACH_ACTIONS, o["still_yd"], casts=casts):
        if r["seconds"] < o["standstill_s"]:
            continue
        ds = [d for s in samples if r["start"] <= s["t"] <= r["end"] and (b := bg.bot_of(s, name))
              and (d := target_dist(s, b)) is not None]
        # the stretch's first sample with the bot (a stretch cut at a cast can start between two samples)
        first = next((s for s in samples if r["start"] <= s["t"] <= r["end"] and bg.bot_of(s, name)), None)
        b0 = bg.bot_of(first, name) if first else {}
        out.append({"bot": name, "start": r["start"], "end": r["end"], "seconds": r["seconds"], "action": r["action"],
                    "x": r["x"], "y": r["y"], "target": b0.get("target") or "",
                    "dist_min": min(ds) if ds else None, "dist_max": max(ds) if ds else None})
    return out


def _cc(records: list[dict], t_pull: float, o: dict) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for r in records:
        if r.get("poly") and r["kind"] in ("start", "go", "fail"):
            by.setdefault(r.get("source") or str(r.get("source_guid") or "?"), []).append(r)
    auras = [r for r in records if r.get("poly") and r["kind"] == "aura"]
    out = []
    for caster, rs in by.items():
        starts = [r for r in rs if r["kind"] == "start"]
        gos = [r for r in rs if r["kind"] == "go"]
        unfinished = []
        for i, st in enumerate(starts):
            nxt = starts[i + 1]["t"] if i + 1 < len(starts) else math.inf
            if not any(st["t"] <= g["t"] < nxt and g["t"] - st["t"] <= o["max_cast_s"] for g in gos):
                unfinished.append(round(st["t"], 1))
        targets = {(g.get("target_guid") or g.get("target") or "").lower() for g in gos}
        landed = [a for a in auras if (a.get("target_guid") or a.get("target") or "").lower() in targets]
        out.append({"caster": caster, "starts": len(starts), "completions": len(gos), "unfinished": unfinished,
                    "failed": sum(1 for r in rs if r["kind"] == "fail"), "landed": len(landed),
                    "targets": sorted({g.get("target") or str(g.get("target_guid") or "") for g in gos}),
                    "first_start_s": round(starts[0]["t"] - t_pull, 1) if starts else None,
                    "first_completion_s": round(gos[0]["t"] - t_pull, 1) if gos else None})
    return out


def _heals(samples: list[dict], records: list[dict], members: list[str], o: dict) -> list[dict]:
    heals = [r for r in records if r["kind"] == "heal"]
    have_events = bool(records)
    out = []
    for m in members:
        prev_good, open_ep = None, None
        for s in samples:
            p = _member_pct(s, m)
            if p is None or _dead(s, m):
                open_ep = None
                continue
            if p >= o["low_pct"]:
                prev_good, open_ep = s["t"], None
                continue
            if open_ep is not None:
                continue
            open_ep = {"member": m, "t": s["t"], "hp": p, "latency_s": None, "by": None, "healed_at": None}
            since = prev_good if prev_good is not None else s["t"]
            h = next((r for r in heals if r["t"] >= since and (r.get("target") or "").lower() == m.lower()), None)
            if h:
                open_ep.update(latency_s=round(max(0.0, h["t"] - s["t"]), 1), by=h.get("source") or None,
                               healed_at=round(h["t"], 1))
            open_ep["events"] = have_events
            out.append(open_ep)
    return sorted(out, key=lambda h: h["t"])


def _deaths(samples: list[dict], records: list[dict], members: list[str], o: dict) -> list[dict]:
    want = {m.lower(): m for m in members}
    found = []
    for r in records:
        who = r.get("source") if r["kind"] == "death" else r.get("target") if r["kind"] == "kill" else None
        if who and who.lower() in want:
            found.append({"name": want[who.lower()], "t": r["t"], "seen": "event"})
    for m in members:
        was_alive = False
        for s in samples:
            if bg.bot_of(s, m) is None:
                continue
            if _dead(s, m):
                if was_alive:
                    found.append({"name": m, "t": s["t"], "seen": "dashboard"})
                was_alive = False
            else:
                was_alive = True
    found.sort(key=lambda d: (d["seen"] != "event", d["t"]))
    deaths = []
    for d in found:  # an event and the dashboard showing the same death: keep the event's time
        if any(x["name"] == d["name"] and abs(x["t"] - d["t"]) <= 3 for x in deaths):
            continue
        deaths.append(d)
    w = o["death_window_s"]
    for d in deaths:
        lo, n = d["t"] - w, d["name"].lower()
        last = []
        for r in records:
            if lo <= r["t"] <= d["t"] and r["kind"] not in ("chat", "death"):
                if (r.get("target") or "").lower() == n:
                    last.append((r["t"], f"{_short(r, 'taken')}"))
                elif (r.get("source") or "").lower() == n:
                    last.append((r["t"], f"{_short(r, 'caused')}"))
        for s in samples:
            if lo <= s["t"] <= d["t"]:
                p = _member_pct(s, d["name"])
                b = bg.bot_of(s, d["name"])
                if p is not None:
                    act = f" {b.get('last_action')}" if b and b.get("last_action") else ""
                    last.append((s["t"], f"hp {p}%{act}"))
        d["last_5s"] = [f"+{t:.1f} {txt}" for t, txt in sorted(last, key=lambda x: x[0])]
    return sorted(deaths, key=lambda d: d["t"])


def _follow(samples: list[dict], names: list[str]) -> list[dict]:
    out = []
    for n in names:
        ds = [d for s in samples if (d := bg.leader_dist(s, n)) is not None]
        if ds:
            out.append({"bot": n, "final_yd": round(ds[-1], 1), "max_yd": round(max(ds), 1),
                        "mean_yd": round(sum(ds) / len(ds), 1)})
    return out


def _metrics(ann: dict, samples: list[dict], records: list[dict]) -> dict:
    still = [a["seconds"] for a in ann["standstill"]]
    healed = [h["latency_s"] for h in ann["heals"] if h["latency_s"] is not None]
    cc = ann["cc"]
    firsts = [c["first_completion_s"] for c in cc if c["first_completion_s"] is not None]
    return {"standstill_s": max(still) if still else (0 if samples else None),
            "first_heal_s": max(healed) if healed else None,
            "unhealed_drops": sum(1 for h in ann["heals"] if h["latency_s"] is None) if records and samples else None,
            "sheep_s": min(firsts) if firsts else ("never" if any(c["starts"] for c in cc) else None),
            "sheep_wasted_casts": sum(c["starts"] - c["completions"] for c in cc) if cc else None,
            "follow_yd": max(f["final_yd"] for f in ann["follow"]) if ann["follow"] else None,
            "deaths": len(ann["deaths"])}


# ---------------------------------------------------------------- the timeline

def annotate(samples: list[dict], records: list[dict], bots: list[str], members: list[str], t_pull: float,
             o: dict) -> dict:
    """The annotations of a window (standstill, cc, heals, deaths, follow) on one clock; no per-second rows."""
    ann = {"standstill": [a for n in bots for a in _standstills(samples, n, o, records)],
           "cc": _cc(records, t_pull, o), "heals": _heals(samples, records, members, o),
           "deaths": _deaths(samples, records, members, o), "follow": _follow(samples, bots)}
    ann["standstill"].sort(key=lambda a: a["start"])
    return ann


def build_timeline(samples: list[dict], records: list[dict], bots: list[str], *, me: str = "",
                   start: float | None = None, end: float | None = None, t_pull: float | None = None,
                   label: str = "", scenario: str = "fight", **opts) -> dict:
    """One row per second from start to end (default: the first and last sample or record), each with every
    bot's action/trigger, movement, target distance, HP/mana and the records it caused or took."""
    o = {**OPTS, **opts}
    ts = [s["t"] for s in samples] + [r["t"] for r in records]
    if start is None:
        start = math.floor(min(ts)) if ts else 0
    if end is None:
        end = max(ts) if ts else start
    start = math.floor(start)
    end = min(end, start + o["max_rows"] - 1)
    samples = sorted((s for s in samples if start <= s["t"] < end + 1), key=lambda s: s["t"])
    records = [r for r in records if start <= r["t"] < end + 1]
    if not me:
        me = next((str(lead["name"]) for s in samples if (lead := s.get("leader")) and lead.get("name")), "")
    t_pull = start if t_pull is None else t_pull
    members = list(bots) + ([me] if me and me.lower() not in {b.lower() for b in bots} else [])

    ann = annotate(samples, records, bots, members, t_pull, o)

    notes: dict[tuple[str, int], list[str]] = {}

    def note(name, t, text):
        notes.setdefault((name.lower(), math.floor(t)), []).append(text)

    for a in ann["standstill"]:
        for k in range(math.floor(a["start"]), math.floor(a["end"]) + 1):
            note(a["bot"], k, "standstill")
    for r in records:
        if r.get("poly") and r["kind"] in ("start", "go"):
            note(r.get("source") or "", r["t"], "cc start" if r["kind"] == "start" else "cc done")
    for h in ann["heals"]:
        note(h["member"], h["t"], f"below {o['low_pct']}%")
    for d in ann["deaths"]:
        note(d["name"], d["t"], "DEATH")

    rows, last, trail = [], {}, {}
    i = 0
    for k in range(int(end - start) + 1):
        t = start + k
        fresh = {}
        while i < len(samples) and samples[i]["t"] < t + 1:
            for n in bots:
                if (b := bg.bot_of(samples[i], n)) is not None:
                    last[n] = (samples[i], b)
                    fresh[n] = True
            i += 1
        in_sec = [r for r in records if t <= r["t"] < t + 1]
        row = {"t": t, "bots": {}, "chat": [f"{r.get('source') or 'system'}: {r.get('text') or ''}"
                                            for r in in_sec if r["kind"] == "chat"]}
        for n in bots:
            nl = n.lower()
            caused = [_short(r, "caused") for r in in_sec
                      if r["kind"] != "chat" and (r.get("source") or "").lower() == nl]
            taken = [_short(r, "taken") for r in in_sec
                     if r["kind"] not in ("chat", "death") and (r.get("target") or "").lower() == nl]
            nts = notes.get((nl, t), [])
            if n not in last:
                row["bots"][n] = ({"action": None, "trigger": None, "state": None, "moved": None, "dist": None,
                                   "lead_yd": None, "hp": None, "mana": None, "target": None, "caused": caused,
                                   "taken": taken, "notes": nts} if caused or taken or nts else None)
                continue
            s, b = last[n]
            # moved: against the oldest position of the last move_window_s (the dashboard snapshots every 2 s,
            # so a 1 s poll often repeats the last position)
            pos = (s["t"], b.get("map"), float(b.get("x") or 0), float(b.get("y") or 0))
            moved = None
            if fresh.get(n):
                hist = trail.setdefault(n, [])
                ref = next((p for p in hist if p[0] >= pos[0] - o["move_window_s"] - 1e-6), None)
                if ref is not None:
                    moved = ref[1] != pos[1] or math.hypot(pos[2] - ref[2], pos[3] - ref[3]) > o["move_yd"]
                hist.append(pos)
                del hist[:-10]
            ld = bg.leader_dist(s, n)
            row["bots"][n] = {"action": b.get("last_action") or "", "trigger": b.get("last_trigger") or "",
                              "state": b.get("state") or "", "moved": moved, "dist": target_dist(s, b),
                              "lead_yd": round(ld, 1) if ld is not None else None,
                              "hp": _pct(b.get("hp"), b.get("max_hp")), "mana": _mana(b),
                              "target": b.get("target") or "", "caused": caused, "taken": taken, "notes": nts}
        rows.append(row)

    return {"kind": "fight-timeline", "label": label, "scenario": scenario, "me": me, "bots": list(bots),
            "start": start, "end": end, "t_pull": t_pull, "samples": len(samples), "records": len(records),
            "options": o, "rows": rows, "annotations": ann, "metrics": _metrics(ann, samples, records)}


# ---------------------------------------------------------------- output

def _f(v, unit=""):
    return "-" if v is None else f"{v:g}{unit}" if isinstance(v, (int, float)) else str(v)


def _cell(xs: list[str], limit: int = 3) -> str:
    more = f" +{len(xs) - limit}" if len(xs) > limit else ""
    return "; ".join(xs[:limit]).replace("|", "/") + more


def annotation_lines(t: dict) -> list[str]:
    """The annotations of a timeline as markdown bullet lines."""
    a, o = t["annotations"], t["options"]
    lines = []
    for s in a["standstill"]:
        d = (f"target {s['target'] or '?'} at {_f(s['dist_min'])}-{_f(s['dist_max'])} yd" if s["dist_min"] is not None
             else f"target {s['target'] or '?'}, distance unknown")
        lines.append(f"- standstill: {s['bot']} stood still {s['seconds']:g} s (t {s['start']:g}-{s['end']:g}) at"
                     f" ({float(s['x'] or 0):.1f},{float(s['y'] or 0):.1f}) with {s['action']!r}, {d}")
    for c in a["cc"]:
        lines.append(f"- cc: {c['caster']} {c['starts']} starts, {c['completions']} completions, {c['landed']} landed,"
                     f" {c['failed']} failed; first start {_f(c['first_start_s'], ' s')}, first completion"
                     f" {_f(c['first_completion_s'], ' s')} after the pull"
                     + (f"; unfinished starts at t {', '.join(f'{x:g}' for x in c['unfinished'])}" if c["unfinished"] else ""))
    for h in a["heals"]:
        what = (f"first heal {h['latency_s']:g} s later by {h['by']}" if h["latency_s"] is not None
                else "no heal seen" if h.get("events") else "no combat events recorded")
        lines.append(f"- heal: {h['member']} below {o['low_pct']}% ({h['hp']}%) at t {h['t']:g}: {what}")
    for d in a["deaths"]:
        lines.append(f"- death: {d['name']} at t {d['t']:g} ({d['seen']}); last {o['death_window_s']:g} s:")
        lines += [f"    - {x}" for x in d["last_5s"]] or ["    - (nothing recorded)"]
    for f in a["follow"]:
        lines.append(f"- follow: {f['bot']} ended {f['final_yd']:g} yd from the player (max {f['max_yd']:g}, mean {f['mean_yd']:g})")
    return lines


def render_markdown(t: dict) -> str:
    m, a, o = t["metrics"], t["annotations"], t["options"]
    out = [f"# Fight timeline {t.get('label') or ''}".rstrip(), "",
           f"- scenario {t['scenario']}, window t {t['start']:g}-{t['end']:.1f} s (pull at {t['t_pull']:g} s),"
           f" {t['samples']} dashboard samples, {t['records']} combat/chat records",
           f"- bots: {', '.join(t['bots'])}" + (f"; player {t['me']}" if t.get("me") else ""),
           "- metrics: " + ", ".join(f"{k} {_f(m.get(k))}" for k in METRIC_KEYS), "", "## Annotations", ""]
    lines = annotation_lines(t)
    out += lines or ["- none"]
    chat = [(r["t"], c) for r in t["rows"] for c in r["chat"]]
    if chat:
        out += ["", "## Chat", ""] + [f"- t {tt:g}: {c}" for tt, c in chat]
    for n in t["bots"]:
        out += ["", f"## {n}", "", "| t | action (trigger) | moved | dist | hp | mana | caused | took | notes |",
                "|---|---|---|---|---|---|---|---|---|"]
        for r in t["rows"]:
            c = r["bots"].get(n)
            if c is None:
                out.append(f"| {r['t']:g} | (no sample) | | | | | | | |")
                continue
            act = (c["action"] or "") + (f" ({c['trigger']})" if c.get("trigger") else "")
            mv = {True: "yes", False: "no", None: "-"}[c["moved"]]
            out.append(f"| {r['t']:g} | {act.replace('|', '/')} | {mv} | {_f(c['dist'], ' yd')} | {_f(c['hp'], '%')}"
                       f" | {_f(c['mana'], '%')} | {_cell(c['caused'])} | {_cell(c['taken'])} | {', '.join(c['notes'])} |")
    return "\n".join(out) + "\n"


def write(t: dict, out_dir: str) -> tuple[str, str]:
    """timeline-<label>.md and .json in out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"timeline-{t.get('label') or 'fight'}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(t, f, indent=1)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(render_markdown(t))
    return base + ".md", base + ".json"


# ---------------------------------------------------------------- live recording

def record_fight(client, dashboard, names: list[str] | None = None, seconds: float = 60, interval_s: float = 1.0,
                 order: str = "", label: str = "", scenario: str = "fight", near_yd: float = 100,
                 wall=time.time, sleep=time.sleep, **opts) -> dict:
    """Sample the dashboard, the player and nearby units every interval_s for `seconds`, send `order` (a GM or
    .bot command, the pull) after the first sample, then read turtle-agent's combat and chat events of the
    window and build the timeline. names: the party bots (default: the party)."""
    from .headless import CHAT_KINDS as HCHAT, COMBAT_KINDS as HCOMBAT, HeadlessError
    st = client.call("status")
    me, seq0, t0 = st.get("character") or "", int(st.get("last_seq") or 0), wall()
    if not names:
        names = [m["name"] for m in (client.call("party").get("members") or []) if m.get("name")]
    samples, dash_errors, t_pull = [], 0, None
    while True:
        now = round(wall() - t0, 2)
        st = client.call("status")
        lead = {"name": me, **{k: st.get(k) for k in ("map", "x", "y", "z", "health", "max_health", "target")}}
        try:
            bots = {str(b.get("name", "")).lower(): b for b in dashboard.bots(names)}
        except DashboardUnavailable:
            dash_errors, bots = dash_errors + 1, {}
        try:
            units = client.call("units", near=near_yd, limit=100) or []
        except HeadlessError:
            units = []
        samples.append({"t": now, "leader": lead, "bots": bots, "units": units})
        if order and t_pull is None:
            client.call("gm", text=order)
            t_pull = round(wall() - t0, 2)
        left = seconds - (wall() - t0)
        if left <= 1e-9:
            break
        sleep(min(interval_s, left))
    evs, seq = [], seq0
    while True:
        page = client.call("events", since=seq, kinds=HCOMBAT + HCHAT, limit=5000)
        evs += page.get("events") or []
        seq = page.get("next", seq)
        if not page.get("more"):
            break
    recs = align(records_from_agent_events(evs, list(names) + [me]), t0)
    out = build_timeline(samples, recs, list(names), me=me, start=0, end=samples[-1]["t"], t_pull=t_pull,
                         label=label, scenario=scenario, **opts)
    out["dashboard_errors"] = dash_errors
    out["order"] = order
    return out

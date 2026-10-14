"""Offline merged timeline of a finished test directory: botwatch.jsonl (dashboard samples) and turtle-agent's
events (events-final.json / events.jsonl / events.json) on one clock, as one time-ordered view.

Inputs
- botwatch.jsonl: {"t": unix, "bots": [dashboard bot dicts]} per line (~/TortoiseBots-audit/tools/botwatch.py);
- events: a list of events {seq, time (RFC3339), kind, source, target, spell_id, amount, text, data}, one JSON
  per line, or the `events` RPC pages ({"events": [...]}, a JSON-RPC {"result": {"events": [...]}}, a list of
  pages). Several files in one directory are merged and de-duplicated by seq.

Both land on epoch seconds (the events' RFC3339 time; the samples' "t"). Each line of interest is printed with
the nearest dashboard sample of the bots it involves (last_action, state, hp %, and the distance between source
and target when both are bots with positions); the annotations are timeline.annotate's (standstill, CC starts vs
completions, first-heal latency, deaths) over the whole run. Pure apart from the file loading.
"""
import bisect
import json
import os
import re
from datetime import datetime, timezone

from . import buildgate as bg
from . import timeline as tl

EVENT_FILES = ("events-final.json", "events.jsonl", "events.json")
SAMPLE_FILE = "botwatch.jsonl"
INTEREST = ("spell_start", "spell_go", "spell_fail", "aura_applied", "aura_removed", "melee", "spell_damage",
            "heal", "death", "kill", "chat", "whisper", "system")
ALWAYS = ("death", "kill", "chat", "whisper", "system")  # shown even when no tracked bot is involved
MAX_GAP_S = 6.0


class RunDirError(Exception):
    pass


# ---------------------------------------------------------------- loading

def _events_of(obj) -> list[dict]:
    """Events out of whatever an events file holds: a list of events, a page, a JSON-RPC reply, a list of pages."""
    if isinstance(obj, list):
        out = []
        for x in obj:
            out += _events_of(x) if isinstance(x, dict) and "kind" not in x else [x]
        return [e for e in out if isinstance(e, dict)]
    if isinstance(obj, dict):
        if "result" in obj and isinstance(obj["result"], (dict, list)):
            return _events_of(obj["result"])
        if isinstance(obj.get("events"), list):
            return _events_of(obj["events"])
        if "kind" in obj:
            return [obj]
    return []


def parse_events(text: str) -> list[dict]:
    """Events from a .json document or from .jsonl lines (a line may itself be a page)."""
    text = text.strip()
    if not text:
        return []
    try:
        return _events_of(json.loads(text))
    except json.JSONDecodeError:
        out = []
        for line in text.splitlines():
            if line.strip():
                out += _events_of(json.loads(line))
        return out


def load_events(run_dir: str) -> list[dict]:
    found = [os.path.join(run_dir, f) for f in EVENT_FILES if os.path.isfile(os.path.join(run_dir, f))]
    if not found:
        raise RunDirError(f"no events file ({', '.join(EVENT_FILES)}) in {run_dir}")
    seen, out = set(), []
    for path in found:
        with open(path, encoding="utf-8") as f:
            for e in parse_events(f.read()):
                key = e.get("seq")
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                out.append(e)
    return sorted((e for e in out if e.get("time")), key=lambda e: (bg.iso_epoch(e["time"]), e.get("seq") or 0))


def parse_samples(text: str) -> list[dict]:
    """botwatch lines as timeline samples (t = unix seconds, bots keyed by lowercase name, no leader/units)."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a half-written last line
        if "t" not in row or not isinstance(row.get("bots"), (list, dict)):
            continue
        bots = row["bots"].values() if isinstance(row["bots"], dict) else row["bots"]
        out.append({"t": float(row["t"]), "leader": None, "units": [],
                    "bots": {str(b.get("name", "")).lower(): b for b in bots if b.get("name")}})
    return sorted(out, key=lambda s: s["t"])


def load_samples(run_dir: str) -> list[dict]:
    path = os.path.join(run_dir, SAMPLE_FILE)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return parse_samples(f.read())


# ---------------------------------------------------------------- clock

def event_tz(evs: list[dict]):
    """The UTC offset the events were stamped with (so --from/--to and the printed times read as the testers' clock)."""
    for e in evs:
        try:
            return datetime.fromisoformat(e["time"].replace("Z", "+00:00")).tzinfo or timezone.utc
        except (ValueError, KeyError):
            continue
    return timezone.utc


def clock_epoch(hms: str, ref_epoch: float, tz) -> float:
    """HH:MM:SS (or HH:MM) on the date of ref_epoch in tz, as epoch seconds."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?", hms.strip())
    if not m:
        raise ValueError(f"bad time {hms!r}, want HH:MM:SS")
    day = datetime.fromtimestamp(ref_epoch, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return day.timestamp() + int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3) or 0)


def fmt_clock(t: float, tz) -> str:
    return datetime.fromtimestamp(t, tz).strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------- merge

def nearest(samples: list[dict], times: list[float], t: float, name: str, max_gap: float = MAX_GAP_S):
    """The sample closest to t that has bot `name`, within max_gap seconds: (sample, bot) or None."""
    i = bisect.bisect_left(times, t)
    for k in sorted(range(max(0, i - 4), min(len(samples), i + 4)), key=lambda k: abs(times[k] - t)):
        if abs(times[k] - t) > max_gap:
            break
        b = bg.bot_of(samples[k], name)
        if b is not None:
            return samples[k], b
    return None


def _spell(e: dict) -> str:
    return f"spell {e['spell_id']}" if e.get("spell_id") else ""


def describe(e: dict) -> str:
    k, src, tgt = e["kind"], e.get("source") or "", e.get("target") or ""
    amt = e.get("amount")
    if k in ("chat", "whisper", "system"):
        who = f"{src}: " if src else ""
        return f"{k} {who}{e.get('text') or ''}".rstrip()
    if k in ("death",):
        return f"death {src or tgt}"
    arrow = f"{src} -> {tgt}" if src and tgt else src or tgt
    detail = " ".join(x for x in (_spell(e), str(amt) if amt else "",
                                  "crit" if e.get("crit") else "",
                                  str((e.get("data") or {}).get("reason") or "")) if x)
    return f"{k} {arrow} {detail}".rstrip()


def dash_note(e: dict, samples, times, bots: set[str]) -> str:
    """Nearest-sample text for the bots involved in e: 'Name: state action hp% [d yd from Other]'."""
    names = []
    for n in (e.get("source"), e.get("target")):
        if n and n.lower() in bots and n.lower() not in [x.lower() for x in names]:
            names.append(n)
    t, parts, found = bg.iso_epoch(e["time"]), [], {}
    for n in names:
        hit = nearest(samples, times, t, n)
        if hit is None:
            parts.append(f"{n}: no sample")
            continue
        s, b = hit
        found[n.lower()] = (s, b)
        hp = tl._pct(b.get("hp"), b.get("max_hp"))
        act = b.get("last_action") or "-"
        trig = f" <- {b['last_trigger']}" if b.get("last_trigger") else ""
        parts.append(f"{n}: {b.get('state') or '?'} {act!r}{trig} hp {hp if hp is not None else '?'}%"
                     f" ({s['t'] - t:+.1f}s)")
    if len(found) == 2:
        (s1, b1), (s2, b2) = found.values()
        if b1.get("map") == b2.get("map"):
            parts.append(f"{bg.dist2d(b1, b2):.1f} yd apart")
    elif len(found) == 1:
        (s, b), = found.values()
        d = tl.target_dist(s, b)
        if d is not None:
            parts.append(f"{d:g} yd from {b.get('target')}")
    return "; ".join(parts)


def merge(evs: list[dict], samples: list[dict], names: list[str], t_from: float | None = None,
          t_to: float | None = None, kinds=INTEREST) -> list[dict]:
    """Time-ordered lines for the events of interest: {"t", "kind", "text", "dash"}."""
    times = [s["t"] for s in samples]
    bots = {n.lower() for n in names}
    out = []
    for e in evs:
        if e["kind"] not in kinds:
            continue
        t = bg.iso_epoch(e["time"])
        if (t_from is not None and t < t_from) or (t_to is not None and t > t_to):
            continue
        involved = (e.get("source") or "").lower() in bots or (e.get("target") or "").lower() in bots
        if not involved and e["kind"] not in ALWAYS and e.get("spell_id") not in bg.POLYMORPH_IDS:
            continue
        out.append({"t": t, "seq": e.get("seq"), "kind": e["kind"], "text": describe(e),
                    "dash": dash_note(e, samples, times, bots) if involved and samples else ""})
    return out


# ---------------------------------------------------------------- the run

def build_run(run_dir: str, names: list[str] | None = None, t_from: str | None = None,
              t_to: str | None = None, **opts) -> dict:
    evs, samples = load_events(run_dir), load_samples(run_dir)
    tz = event_tz(evs)
    if not names:
        names = sorted({b.get("name") for s in samples for b in s["bots"].values() if b.get("name")})
    if not names:  # no dashboard: the party members the agent saw
        names = sorted({e[w] for e in evs if e.get("party") for w in ("source", "target") if e.get(w)
                        and str(e.get(w + "_guid") or "").upper().startswith("0X0000")})  # player guids: high part 0
    ref = min([bg.iso_epoch(e["time"]) for e in evs[:1]] + [s["t"] for s in samples[:1]], default=0.0)
    lo = clock_epoch(t_from, ref, tz) if t_from else None
    hi = clock_epoch(t_to, ref, tz) if t_to else None
    if lo is not None and hi is not None and hi < lo:
        raise ValueError("--to is before --from")
    win_e = [e for e in evs if (lo is None or bg.iso_epoch(e["time"]) >= lo) and (hi is None or bg.iso_epoch(e["time"]) <= hi)]
    win_s = [s for s in samples if (lo is None or s["t"] >= lo) and (hi is None or s["t"] <= hi)]
    ts = [bg.iso_epoch(e["time"]) for e in win_e] + [s["t"] for s in win_s]
    t0 = min(ts) if ts else 0.0
    recs = tl.align(tl.records_from_agent_events(win_e, names), t0)
    asamples = [{**s, "t": round(s["t"] - t0, 3)} for s in win_s]
    o = {**tl.OPTS, **opts}
    # the pull: the harness's own pull order if it was logged, else the first party hit
    pull = next((r["t"] for r in recs if r["kind"] == "chat" and "ACTION_ACK|pull" in (r.get("text") or "")), None)
    if pull is None:
        pull = next((r["t"] for r in recs if r.get("party") and r["kind"] == "damage"), 0.0)
    ann = tl.annotate(asamples, recs, list(names), list(names), pull, o) if ts else \
        {"standstill": [], "cc": [], "heals": [], "deaths": [], "follow": []}
    t = {"kind": "fight-timeline", "label": "", "scenario": "offline", "me": "", "bots": list(names),
         "start": 0, "end": max([r["t"] for r in recs] + [s["t"] for s in asamples] + [0]), "t_pull": pull,
         "samples": len(asamples), "records": len(recs), "options": o, "annotations": ann}
    return {"run_dir": run_dir, "tz": tz, "t0": t0, "names": list(names), "lo": lo, "hi": hi, "events": len(win_e),
            "samples": win_s, "timeline": t, "lines": merge(win_e, win_s, names, lo, hi)}


def render(run: dict) -> str:
    t, tz = run["timeline"], run["tz"]
    out = [f"# Merged timeline {os.path.basename(os.path.abspath(run['run_dir']))}", "",
           f"- clock: {datetime.fromtimestamp(run['t0'], tz).strftime('%Y-%m-%d %H:%M:%S %z')} is t 0 below;"
           f" {run['events']} agent events, {t['samples']} dashboard samples, {len(run['lines'])} lines shown",
           f"- bots: {', '.join(run['names']) or '(none)'}"
           + ("" if t["samples"] else "; no botwatch.jsonl samples, so no dashboard columns or standstill/heal checks"),
           "", "## Annotations (t in seconds from t 0)", ""]
    out += tl.annotation_lines(t) or ["- none"]
    out += ["", "## Merged view", ""]
    for r in run["lines"]:
        out.append(f"{fmt_clock(r['t'], tz)} {r['text']}" + (f" | {r['dash']}" if r["dash"] else ""))
    return "\n".join(out) + "\n"

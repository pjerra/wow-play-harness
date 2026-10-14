"""Build-vs-build comparison of saved build gate results and fight timelines.

    python tools/compare_runs.py out/build13 out/build14
    python tools/compare_runs.py buildgate-b13.json buildgate-b14.json
    python tools/compare_runs.py timeline-b13.json timeline-b14.json --fail-on-worse
    python tools/compare_runs.py a/ b/ --json

Each side is one file, a directory of them, or a comma list of both: build gate results (buildgate-*.json,
tools/buildgate.py) and fight timelines (timeline-*.json, the MCP tool fight_timeline). Per scenario a
directory keeps the latest pass/fail run (else the latest run); a timeline of the same scenario fills in the
numbers the gate does not measure (first-heal latency, deaths, ...).

Prints, per scenario, the pass/fail change and the key numbers - standstill seconds, first-heal latency,
sheep completion time, follow distance, deaths, ... - each with a verdict: better / worse / same within noise
(n/a when one side did not measure it). Exit code: 0, or 1 with --fail-on-worse when anything got worse,
3 on a bad input."""
import argparse
import glob
import json
import os
import sys

# metric: (better when "lower" / "higher", noise band, unit, what it is)
METRICS = {
    "standstill_s": ("lower", 2.0, "s", "longest reach-action standstill"),
    "first_heal_s": ("lower", 1.0, "s", "worst first-heal latency after a drop below 70 %"),
    "unhealed_drops": ("lower", 0, "", "drops below 70 % with no heal"),
    "sheep_s": ("lower", 1.5, "s", "first Polymorph completion after the pull"),
    "sheep_wasted_casts": ("lower", 1, "", "Polymorph starts that did not complete"),
    "party_hits_before_sheep": ("lower", 1, "", "party hits on the Moon mob before the sheep"),
    "follow_yd": ("lower", 3.0, "yd", "final distance to the player"),
    "arrival_s": ("lower", 2.0, "s", "slowest bot into the dungeon"),
    "bots_arrived": ("higher", 0, "", "bots that came into the dungeon"),
    "deaths": ("lower", 0, "", "party deaths"),
    "jumps": ("lower", 0, "", "jump actions in the dungeon"),
}
VERDICT_RANK = {"fail": 0, "pass": 1}


class InputError(Exception):
    pass


# ---------------------------------------------------------------- loading

def gate_metrics(name: str, m: dict) -> dict:
    """The comparable numbers of one build gate scenario's metrics (tools/buildgate.py output)."""
    m = m or {}
    out = {}
    lss = m.get("longest_standstill_s")
    if isinstance(lss, dict) and lss:
        out["standstill_s"] = max(lss.values())
    if m.get("longest_reach_standstill_s") is not None:
        out["standstill_s"] = m["longest_reach_standstill_s"]
    if m.get("final_yd") is not None:
        out["follow_yd"] = m["final_yd"]
    if "polymorph_starts" in m:
        # from the Moon mob's own combat entry when the gate recorded it (the owner's 10 s), else from the pull
        sheep = m.get("first_completion_after_moon_combat_s", m.get("first_completion_s"))
        out["sheep_s"] = sheep if sheep is not None else "never"
        out["sheep_wasted_casts"] = m["polymorph_starts"] - (m.get("polymorph_completions") or 0)
    if m.get("party_hits_before_sheep") is not None:
        out["party_hits_before_sheep"] = m["party_hits_before_sheep"]
    arr = m.get("arrival_s")
    if isinstance(arr, dict):
        out["bots_arrived"] = len(arr)
        if arr:
            out["arrival_s"] = max(arr.values())
    for k in METRICS:  # timeline-shaped numbers a scenario may carry itself
        if k not in out and m.get(k) is not None:
            out[k] = m[k]
    return out


def _files(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        for q in [x for x in p.split(",") if x]:
            if os.path.isdir(q):
                out += sorted(glob.glob(os.path.join(q, "*.json")))
            elif os.path.isfile(q):
                out.append(q)
            else:
                raise InputError(f"{q}: no such file or directory")
    return out


def load_side(paths: list[str]) -> dict:
    """{"label", "files", "scenarios": {name: {"verdict", "metrics", "runs"}}} from gate results and timelines."""
    files = _files(paths)
    gate_runs: dict[str, list[tuple]] = {}
    timelines: dict[str, dict] = {}
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError) as err:
            raise InputError(f"{f}: {err}") from None
        if d.get("kind") == "fight-timeline":
            timelines[d.get("scenario") or "fight"] = {k: v for k, v in (d.get("metrics") or {}).items() if v is not None}
        elif d.get("gate") == "tortoisebots-buildgate":
            for sc in d.get("scenarios") or []:
                gate_runs.setdefault(sc["name"], []).append((d.get("started") or "", d.get("label") or os.path.basename(f), sc))
        # other JSON in the directory (configs, notes) is not a run: skipped
    scenarios = {}
    for name, runs in gate_runs.items():
        runs.sort(key=lambda r: r[0])
        decisive = [r for r in runs if r[2]["verdict"] in VERDICT_RANK]
        _, _, sc = (decisive or runs)[-1]
        scenarios[name] = {"verdict": sc["verdict"], "metrics": gate_metrics(name, sc.get("metrics")),
                           "runs": [f"{lab} {r['verdict']}" for _, lab, r in runs]}
    for name, m in timelines.items():
        sc = scenarios.setdefault(name, {"verdict": None, "metrics": {}, "runs": ["timeline"]})
        sc["metrics"] = {**m, **sc["metrics"]}
    if len(paths) == 1 and os.path.isdir(paths[0]):
        label = os.path.basename(os.path.normpath(paths[0]))
    elif len(files) == 1:
        with open(files[0], encoding="utf-8") as fh:
            label = json.load(fh).get("label") or os.path.basename(files[0])
    else:
        label = "+".join(os.path.basename(os.path.normpath(p)) for p in paths)
    return {"label": label, "files": len(files), "scenarios": scenarios}


# ---------------------------------------------------------------- judging

def judge(metric: str, a, b) -> str:
    """better / worse / same within noise / n/a. "never" (the event never happened) is worse than any time."""
    if a is None or b is None:
        return "n/a"
    direction, noise = METRICS[metric][0], METRICS[metric][1]
    if a == "never" or b == "never":
        return "same within noise" if a == b else "better" if a == "never" else "worse"
    d = float(b) - float(a)
    if abs(d) <= noise + 1e-9:
        return "same within noise"
    return "better" if (d < 0) == (direction == "lower") else "worse"


def judge_verdict(a, b) -> str:
    if a == b:
        return "same"
    if a not in VERDICT_RANK or b not in VERDICT_RANK:
        return "n/a"
    return "better" if VERDICT_RANK[b] > VERDICT_RANK[a] else "worse"


def compare(a: dict, b: dict) -> dict:
    names = list(a["scenarios"]) + [n for n in b["scenarios"] if n not in a["scenarios"]]
    out, summary = [], {"better": 0, "worse": 0, "same": 0, "n/a": 0}
    for n in names:
        sa, sb = a["scenarios"].get(n), b["scenarios"].get(n)
        if not sa or not sb:
            continue
        verdict = None
        if sa["verdict"] or sb["verdict"]:
            v = judge_verdict(sa["verdict"], sb["verdict"])
            verdict = (sa["verdict"], sb["verdict"], v)
            summary["same" if v == "same" else v] += 1
        rows = []
        for k in METRICS:
            va, vb = sa["metrics"].get(k), sb["metrics"].get(k)
            if va is None and vb is None:
                continue
            j = judge(k, va, vb)
            summary["same" if j == "same within noise" else j] += 1
            rows.append({"metric": k, "a": va, "b": vb, "judge": j})
        out.append({"name": n, "verdict": verdict, "metrics": rows, "runs": (sa.get("runs"), sb.get("runs"))})
    return {"a": a["label"], "b": b["label"], "files": (a["files"], b["files"]), "scenarios": out,
            "only_a": [n for n in a["scenarios"] if n not in b["scenarios"]],
            "only_b": [n for n in b["scenarios"] if n not in a["scenarios"]], "summary": summary}


# ---------------------------------------------------------------- output

def _v(x) -> str:
    return "-" if x is None else x if isinstance(x, str) else f"{x:g}"


def render(d: dict) -> str:
    out = [f"compare {d['a']} -> {d['b']} ({d['files'][0]} files -> {d['files'][1]} files)"]
    for s in d["scenarios"]:
        out.append("")
        if s["verdict"]:
            va, vb, j = s["verdict"]
            out.append(f"{s['name']}: {(va or '-').upper()} -> {(vb or '-').upper()}  {j}")
        else:
            out.append(f"{s['name']}: (timeline)")
        for r in s["metrics"]:
            unit = METRICS[r["metric"]][2]
            delta = ""
            if isinstance(r["a"], (int, float)) and isinstance(r["b"], (int, float)):
                delta = f"{r['b'] - r['a']:+.1f}" if r["b"] != r["a"] else "0"
                delta = delta[:-2] if delta.endswith(".0") else delta
            vals = f"{_v(r['a'])} -> {_v(r['b'])}" + (f" {unit}" if unit else "")
            noise = METRICS[r["metric"]][1]
            j = r["judge"] + (f" (+/-{noise:g}{(' ' + unit) if unit else ''})" if r["judge"] == "same within noise" and noise else "")
            out.append(f"  {r['metric']:<24} {vals:<20} {delta:>6}  {j}")
    for side, key in ((d["a"], "only_a"), (d["b"], "only_b")):
        if d[key]:
            out.append("")
            out.append(f"only in {side}: {', '.join(d[key])}")
    sm = d["summary"]
    out += ["", f"summary: {sm['better']} better, {sm['worse']} worse, {sm['same']} same within noise, {sm['n/a']} n/a"]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a", help="the old build: a result file, a directory, or a comma list")
    ap.add_argument("b", help="the new build: same forms")
    ap.add_argument("--json", action="store_true", help="print the comparison as JSON")
    ap.add_argument("--fail-on-worse", action="store_true", help="exit 1 when any number or verdict got worse")
    a = ap.parse_args(argv)
    try:
        d = compare(load_side([a.a]), load_side([a.b]))
    except InputError as err:
        print(f"compare_runs: {err}", file=sys.stderr)
        return 3
    print(json.dumps(d, indent=1) if a.json else render(d))
    return 1 if a.fail_on_worse and d["summary"]["worse"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

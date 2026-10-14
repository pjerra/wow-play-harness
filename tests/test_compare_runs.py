"""tools/compare_runs.py: build-vs-build diff of saved build gate results and fight timelines. The gate
results are tonight's real b13 runs (tests/fixtures/compare); the timelines are built from the recorded
repro-124 samples (tests/fixtures/buildgate). No live runs."""
import copy
import json
import os

from tools import compare_runs as cr
from wowharness import timeline as tl

HERE = os.path.dirname(__file__)
CMP = os.path.join(HERE, "fixtures", "compare")
FIRST, RERUN = os.path.join(CMP, "b13-first"), os.path.join(CMP, "b13-rerun")
R124 = os.path.join(HERE, "fixtures", "buildgate", "r124c_pull1_samples.json")
BOTS = ["Thormora", "Rarrurdyr", "Sikorstae"]


def r124_samples():
    with open(R124, encoding="utf-8") as f:
        return json.load(f)["samples"]


def fixed_samples():
    """The same pull with the casters closing in instead of standing on 'reach spell' (the fix we want)."""
    s = copy.deepcopy(r124_samples())
    for smp in s:
        for n in ("rarrurdyr", "sikorstae"):
            b = smp["bots"][n]
            if "reach spell" in (b.get("last_action") or ""):
                b["x"] = b["x"] - 2 * smp["t"]  # moving every sample
    return s


# ---------------------------------------------------------------- loading

def test_gate_result_metrics_from_the_real_b13_runs():
    run = cr.load_side([os.path.join(RERUN, "buildgate-b13-s6-moon-r2.json")])
    moon = run["scenarios"]["moon_sheep_lands"]
    assert moon["verdict"] == "fail"
    assert moon["metrics"] == {"sheep_s": 9.8, "sheep_wasted_casts": 0, "party_hits_before_sheep": 2}
    run = cr.load_side([RERUN])
    assert run["scenarios"]["ranged_reach_in_dungeon"]["metrics"] == {"standstill_s": 0}
    assert run["scenarios"]["follow_into_dungeon"]["metrics"] == {"arrival_s": 9.8, "bots_arrived": 4}
    assert run["scenarios"]["stay_then_order_follows"]["metrics"] == {"follow_yd": 29.7}
    assert run["scenarios"]["no_jumps_in_dungeon"]["metrics"] == {"jumps": 0}
    first = cr.load_side([FIRST])
    assert first["scenarios"]["healer_follows_ooc"]["metrics"] == {"standstill_s": 0, "follow_yd": 2.3}
    assert first["scenarios"]["moon_sheep_lands"]["metrics"] == {}  # inconclusive: nothing measured
    assert first["files"] == 5 and first["label"] == "b13-first"


def _gate(label, started, verdict, metrics, name="moon_sheep_lands"):
    return {"gate": "tortoisebots-buildgate", "label": label, "started": started, "verdict": verdict,
            "scenarios": [{"name": name, "verdict": verdict, "evidence": [], "metrics": metrics}]}


def test_a_directory_keeps_the_latest_decisive_run_per_scenario(tmp_path):
    runs = [_gate("r1", "2026-10-12T15:00:00+00:00", "fail", {"polymorph_starts": 3, "polymorph_completions": 0,
                                                              "first_completion_s": None}),
            _gate("r2", "2026-10-12T15:05:00+00:00", "pass", {"polymorph_starts": 1, "polymorph_completions": 1,
                                                              "first_completion_s": 4.0}),
            _gate("r3", "2026-10-12T15:10:00+00:00", "inconclusive", {})]
    for r in runs:
        (tmp_path / f"buildgate-{r['label']}.json").write_text(json.dumps(r))
    sc = cr.load_side([str(tmp_path)])["scenarios"]["moon_sheep_lands"]
    assert sc["verdict"] == "pass" and sc["metrics"]["sheep_s"] == 4.0
    assert sc["runs"] == ["r1 fail", "r2 pass", "r3 inconclusive"]
    first = cr.load_side([str(tmp_path / "buildgate-r1.json")])["scenarios"]["moon_sheep_lands"]["metrics"]
    assert first == {"sheep_s": "never", "sheep_wasted_casts": 3}


def test_timeline_files_load_as_their_scenario_and_fill_in_gate_metrics(tmp_path):
    t = tl.build_timeline(r124_samples(), [], BOTS, label="r124", scenario="ranged_reach_in_dungeon")
    tl.write(t, str(tmp_path))
    (tmp_path / "buildgate-x.json").write_text(json.dumps(
        _gate("x", "2026-10-12T15:00:00+00:00", "fail", {"longest_standstill_s": {"Rarrurdyr": 25}},
              name="ranged_reach_in_dungeon")))
    sc = cr.load_side([str(tmp_path)])["scenarios"]["ranged_reach_in_dungeon"]
    assert sc["verdict"] == "fail"
    assert sc["metrics"]["standstill_s"] == 25  # the gate's own number wins
    assert sc["metrics"]["deaths"] == 0 and "first_heal_s" not in sc["metrics"]  # filled from the timeline, Nones left out


# ---------------------------------------------------------------- judging

def test_judge_rules():
    assert cr.judge("standstill_s", 26, 3) == "better"
    assert cr.judge("standstill_s", 3, 26) == "worse"
    assert cr.judge("standstill_s", 5, 6.5) == "same within noise"
    assert cr.judge("follow_yd", 30.5, 29.7) == "same within noise"
    assert cr.judge("deaths", 0, 1) == "worse"
    assert cr.judge("bots_arrived", 3, 4) == "better"
    assert cr.judge("sheep_s", "never", 9.8) == "better"
    assert cr.judge("sheep_s", 9.8, "never") == "worse"
    assert cr.judge("sheep_s", "never", "never") == "same within noise"
    assert cr.judge("sheep_s", None, 9.8) == "n/a"
    assert cr.judge_verdict("fail", "pass") == "better" and cr.judge_verdict("pass", "fail") == "worse"
    assert cr.judge_verdict("pass", "pass") == "same" and cr.judge_verdict("inconclusive", "pass") == "n/a"


# ---------------------------------------------------------------- the diff

def test_compare_the_real_b13_first_runs_with_the_reruns():
    d = cr.compare(cr.load_side([FIRST]), cr.load_side([RERUN]))
    by = {s["name"]: s for s in d["scenarios"]}
    assert by["follow_into_dungeon"]["verdict"] == ("fail", "pass", "better")
    m = {x["metric"]: x for x in by["follow_into_dungeon"]["metrics"]}
    assert (m["bots_arrived"]["a"], m["bots_arrived"]["b"], m["bots_arrived"]["judge"]) == (3, 4, "better")
    assert m["arrival_s"]["judge"] == "worse"  # 5.3 -> 9.8 s
    stay = {x["metric"]: x["judge"] for x in by["stay_then_order_follows"]["metrics"]}
    assert stay == {"follow_yd": "same within noise"}
    assert by["moon_sheep_lands"]["verdict"] == ("inconclusive", "fail", "n/a")
    assert {x["metric"]: x["judge"] for x in by["moon_sheep_lands"]["metrics"]} == \
        {"sheep_s": "n/a", "sheep_wasted_casts": "n/a", "party_hits_before_sheep": "n/a"}
    assert d["only_a"] == ["healer_follows_ooc"] and d["only_b"] == ["no_jumps_in_dungeon"]
    assert d["summary"]["better"] == 2 and d["summary"]["worse"] == 1


def test_render_is_short_and_has_a_verdict_per_metric():
    text = cr.render(cr.compare(cr.load_side([FIRST]), cr.load_side([RERUN])))
    assert text.splitlines()[0] == "compare b13-first -> b13-rerun (5 files -> 5 files)"
    line = next(l for l in text.splitlines() if "bots_arrived" in l)
    assert "3 -> 4" in line and line.rstrip().endswith("better")
    assert any("follow_yd" in l and "30.5 -> 29.7 yd" in l and "same within noise" in l for l in text.splitlines())
    assert "only in b13-first: healer_follows_ooc" in text and "only in b13-rerun: no_jumps_in_dungeon" in text
    assert text.splitlines()[-1].startswith("summary: 2 better, 1 worse")
    assert len(text.splitlines()) < 40


def test_cli_on_two_timelines(tmp_path, capsys):
    a = tl.build_timeline(r124_samples(), [], BOTS, label="build13", scenario="ranged_reach_in_dungeon")
    b = tl.build_timeline(fixed_samples(), [], BOTS, label="build14", scenario="ranged_reach_in_dungeon")
    _, ja = tl.write(a, str(tmp_path / "b13"))
    _, jb = tl.write(b, str(tmp_path / "b14"))
    assert cr.main([ja, jb]) == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "standstill_s" in l)
    assert "26 -> 0 s" in line and line.rstrip().endswith("better")
    assert cr.main([jb, ja, "--fail-on-worse"]) == 1
    capsys.readouterr()
    assert cr.main([ja, jb, "--json"]) == 0
    js = json.loads(capsys.readouterr().out)
    assert js["summary"]["better"] == 1


def test_cli_reports_a_bad_path(capsys, tmp_path):
    assert cr.main([str(tmp_path / "nope.json"), FIRST]) == 3
    assert "nope.json" in capsys.readouterr().err

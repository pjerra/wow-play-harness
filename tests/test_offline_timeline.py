import json
import os
import subprocess
import sys

import pytest

from wowharness import offline_timeline as ot

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "run_timeline")
ROOT = os.path.dirname(os.path.dirname(__file__))


def test_parse_events_accepts_every_shape():
    ev = {"seq": 1, "time": "2026-10-12T19:54:13+02:00", "kind": "system", "text": "x"}
    page = {"events": [ev], "next": 1, "more": False}
    assert ot.parse_events(json.dumps([ev])) == [ev]
    assert ot.parse_events(json.dumps(page)) == [ev]
    assert ot.parse_events(json.dumps({"jsonrpc": "2.0", "id": 1, "result": page})) == [ev]
    assert ot.parse_events(json.dumps([page, page])) == [ev, ev]
    assert ot.parse_events(json.dumps(ev) + "\n" + json.dumps(ev) + "\n") == [ev, ev]
    assert ot.parse_events(json.dumps(page) + "\n") == [ev]
    assert ot.parse_events("") == []


def test_parse_samples_keys_by_name_and_skips_half_written_line():
    text = open(os.path.join(FIX, "botwatch.jsonl")).read()
    ss = ot.parse_samples(text)
    assert len(ss) == 10 and set(ss[0]["bots"]) == {"mage", "tank", "priest"}
    assert ss == sorted(ss, key=lambda s: s["t"])


def test_load_events_merges_and_dedupes_by_seq(tmp_path):
    e = lambda n, t: {"seq": n, "time": f"2026-10-12T19:54:{t:02d}+02:00", "kind": "system", "text": str(n)}
    (tmp_path / "events.json").write_text(json.dumps([e(2, 5), e(1, 1)]))
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(x) for x in (e(2, 5), e(3, 9))))
    assert [x["seq"] for x in ot.load_events(str(tmp_path))] == [1, 2, 3]


def test_missing_events_is_a_clear_error(tmp_path):
    with pytest.raises(ot.RunDirError):
        ot.build_run(str(tmp_path))


def test_clock_uses_the_events_offset():
    evs = ot.load_events(FIX)
    tz = ot.event_tz(evs)
    t = ot.clock_epoch("19:54:13", evs[0]["time"] and ot.bg.iso_epoch(evs[0]["time"]), tz)
    assert ot.fmt_clock(t, tz) == "19:54:13.000"
    assert ot.bg.iso_epoch(evs[0]["time"]) == t


def test_merge_pairs_events_with_nearest_sample():
    run = ot.build_run(FIX)
    by = {r["seq"]: r for r in run["lines"]}
    assert run["names"] == ["Mage", "Priest", "Tank"]
    assert 6 not in by  # periodic energize is not a line of interest
    assert "Mage: combat 'reach spell'" in by[2]["dash"]
    assert by[5]["dash"].endswith("4 yd from Mage") or "4 yd from Mage" in by[5]["dash"]  # Tank's target is Mage
    assert "5.0 yd apart" in by[7]["dash"] and "Priest" in by[7]["dash"] and "Tank" in by[7]["dash"]
    assert by[9]["dash"] == "" and by[9]["text"] == "death Ruffian"
    assert [r["t"] for r in run["lines"]] == sorted(r["t"] for r in run["lines"])


def test_annotations_reuse_timeline_logic():
    ann = ot.build_run(FIX)["timeline"]["annotations"]
    assert [a["bot"] for a in ann["standstill"]] == ["Priest", "Mage"]
    assert ann["cc"][0]["starts"] == 1 and ann["cc"][0]["completions"] == 1 and ann["cc"][0]["failed"] == 1
    assert ann["cc"][0]["first_start_s"] == 2.2  # relative to the logged pull order, not the first event
    h = ann["heals"][0]
    assert h["member"] == "Tank" and h["by"] == "Priest" and h["latency_s"] == 3.6


def test_time_window_and_names():
    run = ot.build_run(FIX, names=["Mage"], t_from="19:54:15", t_to="19:54:19")
    assert [r["seq"] for r in run["lines"]] == [2, 3, 4]  # Tank-only melee (5) is outside the window; aura 4 is poly
    assert run["names"] == ["Mage"]
    with pytest.raises(ValueError):
        ot.build_run(FIX, t_from="19:54:20", t_to="19:54:15")
    with pytest.raises(ValueError):
        ot.build_run(FIX, t_from="nonsense")


def test_agent_only_run_has_no_dashboard_columns(tmp_path):
    (tmp_path / "events.jsonl").write_text(open(os.path.join(FIX, "events.jsonl")).read())
    run = ot.build_run(str(tmp_path))
    assert run["names"] == ["Mage", "Priest", "Tank"]  # player guids in the party-flagged events
    assert all(r["dash"] == "" for r in run["lines"])
    assert "no botwatch.jsonl samples" in ot.render(run)


def test_cli_writes_out_file(tmp_path):
    out = tmp_path / "timeline.md"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "run_timeline.py"), FIX, "--names", "Mage,Tank",
                        "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    assert "19:54:15.200 spell_start Mage -> Ruffian spell 118 | Mage: combat 'reach spell'" in text
    bad = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "run_timeline.py"), str(tmp_path)],
                         capture_output=True, text=True)
    assert bad.returncode == 2 and "no events file" in bad.stderr

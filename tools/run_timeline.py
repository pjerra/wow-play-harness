"""Merged offline timeline of a finished test run directory.

    python tools/run_timeline.py ~/TortoiseBots-audit/tests/gate-b17
    python tools/run_timeline.py <run dir> --names Bronomoro,Sorcorzos --from 20:01:00 --to 20:03:30 --out timeline.md

Reads <run dir>/botwatch.jsonl (dashboard samples, TortoiseBots-audit tools/botwatch.py) and turtle-agent's
events (events-final.json, events.jsonl, events.json; merged by seq), puts both on one clock and prints, time
ordered, one line per spell start/go/fail, aura applied/removed, melee, heal, death, kill and chat/system event with
the nearest dashboard sample of the bots involved (state, last_action, hp %, distance source-target), preceded by
the fight-timeline annotations (standstill, CC, first heal, deaths). --from/--to are HH:MM:SS on the events' own
clock. Without botwatch.jsonl only the agent side is shown. --out writes the text to a file instead of stdout.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from wowharness import offline_timeline as ot  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("run_dir")
    p.add_argument("--names", default="", help="comma list of party bots (default: those in botwatch.jsonl)")
    p.add_argument("--from", dest="t_from", help="HH:MM:SS, start of the window")
    p.add_argument("--to", dest="t_to", help="HH:MM:SS, end of the window")
    p.add_argument("--out", help="write here instead of stdout")
    a = p.parse_args(argv)
    try:
        run = ot.build_run(a.run_dir, [n.strip() for n in a.names.split(",") if n.strip()], a.t_from, a.t_to)
    except (ot.RunDirError, ValueError, OSError) as e:
        print(f"run_timeline: {e}", file=sys.stderr)
        return 2
    text = ot.render(run)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {a.out} ({len(run['lines'])} lines)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

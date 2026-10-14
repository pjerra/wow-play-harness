"""TortoiseBots build gate: headless regression scenarios after a deploy (turtle-agent + bot dashboard).

    python tools/buildgate.py --config tools/buildgate_uldaman.json --label build14
    python tools/buildgate.py --config tools/buildgate_uldaman.json --dry-run
    python tools/buildgate.py --config ... --scenarios healer_follows_ooc,no_jumps_in_dungeon --no-logout

turtle-agent must already be logged in with its RPC on --rpc; it keeps the account credentials in its own
settings file and this tool never reads them. The dashboard login comes from BUILDGATE_DASHBOARD_URL /
_USER / _PASSWORD, else the dashboard_* keys of --dashboard-config (default ~/.wowharness.json; no other
key of that file is used).

Writes <out-dir>/buildgate-<label>.md and .json. Exit code: 0 pass, 1 fail, 2 inconclusive, 3 setup error."""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from wowharness import buildgate as bg  # noqa: E402
from wowharness.headless import DEFAULT_RPC  # noqa: E402

EXIT = {bg.PASS: 0, bg.FAIL: 1, bg.INCONCLUSIVE: 2}


def dashboard_settings(url: str | None, path: str | None) -> tuple[str, str, str]:
    """(url, user, password) from the environment, else only the dashboard_* keys of the settings file."""
    env = os.environ
    found = {"dashboard_url": env.get("BUILDGATE_DASHBOARD_URL", ""), "dashboard_user": env.get("BUILDGATE_DASHBOARD_USER", ""),
             "dashboard_password": env.get("BUILDGATE_DASHBOARD_PASSWORD", "")}
    path = path or os.path.join(os.path.expanduser("~"), ".wowharness.json")
    if not all(found.values()) and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        for k in found:
            found[k] = found[k] or str(raw.get(k) or "")
    return url or found["dashboard_url"], found["dashboard_user"], found["dashboard_password"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="scenario config (spots, entries, thresholds)")
    ap.add_argument("--scenarios", help="comma list (default: all, in gate order): " + ",".join(bg.ORDER))
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    ap.add_argument("--dashboard-url")
    ap.add_argument("--dashboard-config", help="JSON file with dashboard_url/_user/_password (default ~/.wowharness.json)")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--label", default="", help="report name, e.g. the build (default: a timestamp)")
    ap.add_argument("--no-logout", action="store_true", help="keep the character and party in world at the end")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit; no network")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = bg.load_gate_config(a.config)
    except (OSError, ValueError, KeyError) as err:
        print(f"config: {err}", file=sys.stderr)
        return 3
    names = [n.strip() for n in (a.scenarios or ",".join(bg.ORDER)).split(",") if n.strip()]
    unknown = [n for n in names if n not in bg.ORDER]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}; known: {', '.join(bg.ORDER)}", file=sys.stderr)
        return 3
    if a.dry_run:
        print("\n".join(bg.plan(cfg, names, logout=not a.no_logout)))
        return 0

    from wowharness.dashboard import Dashboard
    from wowharness.headless import HeadlessClient

    url, user, password = dashboard_settings(a.dashboard_url, a.dashboard_config)
    if not url:
        print("no dashboard URL (--dashboard-url, BUILDGATE_DASHBOARD_URL or dashboard_url in the settings file)", file=sys.stderr)
        return 3
    gate = bg.Gate(HeadlessClient(a.rpc), Dashboard(url, user, password), cfg,
                   log=(lambda m: None) if a.quiet else (lambda m: print(m, flush=True)))
    label = a.label or time.strftime("%Y%m%d-%H%M%S")
    rep = gate.run(names, logout=not a.no_logout, label=label)
    os.makedirs(a.out_dir, exist_ok=True)
    base = os.path.join(a.out_dir, f"buildgate-{label}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2)
    md = bg.render_markdown(rep)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(md)
    if not a.quiet:
        print(md)
    print(f"{rep['verdict'].upper()}: {base}.md, {base}.json")
    return EXIT[rep["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())

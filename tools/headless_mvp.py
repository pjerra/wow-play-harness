"""The harness MVP scenario without a game window: teleport to --at (GM still on), GM off and visible,
two bots in the party, target and attack a living mob, then the same fight report the real-client
harness prints. Whatever happens after the first status call, it ends safe: stop attacking, GM on,
leave the group, and log out through turtle-agent (a logout refused in combat is retried).

turtle-agent must already be logged in with its RPC on --rpc. This script takes no credentials:
turtle-agent reads them from its own settings file when it logs in. The bots are --bots, else the
settings file's "bots" list (the only key read from it), else companions hired with --hire.

Exit codes: 0 fight reported, 2 the bots did not join, 3 turtle-agent error, 4 no living mob to fight."""
import argparse
import json
import math
import os
import time

from wowharness.headless import DEFAULT_RPC, HeadlessClient, HeadlessError, load_spell_names

MOB_WAIT_S = 10  # after a teleport the mob's create block and name can take a moment (ruling P8)
MELEE_YD = 2.0  # walk to this far short of the mob before attacking
# CMSG_SET_SELECTION and CMSG_MESSAGECHAT run in different server queues (Opcodes.cpp:205, :373): give the
# selection this long before a .bot order that reads it, or the bots see no target.
SELECTION_SETTLE_S = 1.0
LOGOUT_RETRY_S = 30  # a logout refused in combat is retried this long, every LOGOUT_RETRY_EVERY_S
LOGOUT_RETRY_EVERY_S = 2
SETTINGS = os.path.join(os.path.expanduser("~"), ".config", "turtle-agent", "settings.json")
HIRE = ["warrior tank", "priest holy"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    ap.add_argument("--bots", nargs=2, metavar=("BOT1", "BOT2"), help="default: the settings' bots, else --hire")
    ap.add_argument("--settings", default=SETTINGS, help="turtle-agent's settings file; only its bots list is read")
    ap.add_argument("--hire", nargs=2, default=HIRE, metavar=("SPEC1", "SPEC2"),
                    help="'.bot hire <class> <role>' specs when no bots are named (default: %(default)s)")
    ap.add_argument("--mob", required=True, help="creature name to attack (nearest living match)")
    ap.add_argument("--at", nargs=4, type=float, metavar=("X", "Y", "Z", "MAP"),
                    help="GM-teleport here first, before .gm off: it must be a safe spot")
    ap.add_argument("--fight-s", type=float, default=60, help="stop waiting for the kill after this long")
    ap.add_argument("--spell-names", help="JSON file mapping spell id to name for the report")
    ap.add_argument("--wait-s", type=float, default=3.0, help="how long to collect replies to each command")
    a = ap.parse_args(argv)
    try:
        return _run(a)
    except HeadlessError as err:
        print("turtle-agent:", err)
        return 3


def settings_bots(path: str) -> list[str]:
    """The settings file's "bots" list, or [] when the file or the key is missing. Nothing else in the
    file (it holds the account passwords) is kept or printed."""
    try:
        with open(path, encoding="utf-8") as f:
            return [str(b) for b in json.load(f).get("bots") or []]
    except (OSError, ValueError, AttributeError):
        return []


def _members(c) -> list[str]:
    return [m["name"] for m in c.call("party").get("members") or []]


def _party(c, a) -> list[str] | None:
    """The bots for the fight: named ones added and invited unless already in the party, else hired
    companions (only the specs the party is still short of). None when they did not join in 30 s."""
    bots = list(a.bots or settings_bots(a.settings))
    members = _members(c)
    if bots:
        for b in bots:
            if b not in members:
                c.bot(f"add {b}", wait_s=a.wait_s)
                c.call("invite", name=b)
        ready = lambda m: all(b in m for b in bots)
    else:
        for spec in a.hire[len(members):]:
            print(f".bot hire {spec} ->", " | ".join(c.bot(f"hire {spec}", wait_s=a.wait_s)["lines"]) or "(no reply)")
        ready = lambda m: len(m) >= len(a.hire)
    deadline = time.monotonic() + 30
    while not ready(members := _members(c)):
        if time.monotonic() > deadline:
            return None
        time.sleep(1)
    return bots or members[:len(a.hire)]


def _close_in(c, mob: dict) -> None:
    """Walk to MELEE_YD short of the mob (straight line, so it must be within turtle-agent's walk range)
    and face it."""
    st = c.call("status")
    dx, dy = mob["x"] - st["x"], mob["y"] - st["y"]
    dist = math.hypot(dx, dy)
    if dist > MELEE_YD + 0.5:
        k = (dist - MELEE_YD) / dist
        c.call("walk_to", x=round(st["x"] + dx * k, 2), y=round(st["y"] + dy * k, 2), z=mob["z"])
    c.call("face", guid=mob["guid"])


def _run(a) -> int:
    names = load_spell_names(a.spell_names) if a.spell_names else {}
    c = HeadlessClient(a.rpc, spell_names=names)
    me = c.call("status")["character"]
    try:
        return _fight(c, a, me)
    finally:
        _end_safe(c, a)


def _fight(c, a, me: str) -> int:
    # Safe-spot ruling (15:3x): teleport while GM is still on, so the character never stands GM-off at
    # the login spot, which may be dangerous, while it waits up to 30 s for the bots.
    if a.at:
        c.call("teleport", x=a.at[0], y=a.at[1], z=a.at[2], map=int(a.at[3]))
    for cmd in (".gm off", ".gm visible on"):
        print(cmd, "->", c.gm(cmd, wait_s=a.wait_s)["reply"] or "(no reply)")
    bots = _party(c, a)
    if bots is None:
        print("the bots did not join the party within 30 s")
        return 2
    print("party:", ", ".join(bots))
    deadline = time.monotonic() + MOB_WAIT_S
    while not c.call("units", name=a.mob, limit=1) and time.monotonic() < deadline:
        time.sleep(0.5)
    combat0, chat0 = c.tail_combat_log()["offset"], c.tail_chat()["offset"]
    mob = c.call("target", name=a.mob)  # turtle-agent picks the nearest living match
    targeted = time.monotonic()
    print("target:", mob.get("name"), mob.get("guid"), f"{mob.get('health')}/{mob.get('max_health')}")
    if mob.get("dead"):
        print(f"no living {a.mob} in view, only a corpse: nothing to fight")
        return 4
    _close_in(c, mob)
    c.call("attack")
    time.sleep(max(0.0, targeted + SELECTION_SETTLE_S - time.monotonic()))
    print(".bot action attack ->", " | ".join(c.bot("action attack", wait_s=a.wait_s)["lines"]) or "(no reply)")
    deadline = time.monotonic() + a.fight_s
    while time.monotonic() < deadline:
        # Follow the targeted unit by GUID: a nearer corpse or a second mob of the same name is not it.
        seen = [u for u in c.call("units", name=a.mob) if u.get("guid") == mob["guid"]]
        if not seen or seen[0].get("dead"):
            break
        time.sleep(1)
    rep = c.fight_report(combat0, chat0, me, bots)
    print(rep["summary"])
    print(json.dumps(rep["report"], indent=2))
    return 0


def _end_safe(c, a) -> None:
    """Leave the character safe whatever happened: stop attacking, GM on, leave the group, then log out
    through turtle-agent. GM goes on before the group is left, so the companions are never dismissed in
    combat while the character is GM-off. Every step runs even when one before it failed; a logout the
    server refuses (in combat) is retried for LOGOUT_RETRY_S."""
    for step, do in (("stop_attack", lambda: c.call("stop_attack")), (".gm on", lambda: c.gm(".gm on", wait_s=a.wait_s)),
                     ("leave_group", lambda: c.call("leave_group"))):
        try:
            do()
        except HeadlessError as err:
            print(f"end safe: {step}: {err}")
    deadline = time.monotonic() + LOGOUT_RETRY_S
    while True:
        try:
            c.call("logout")
            print("end safe: logged out")
            return
        except HeadlessError as err:
            refused = err.code == -32003 and "logout refused" in str(err)
            if not refused or time.monotonic() >= deadline:
                print("end safe: logout:", err)
                return
            time.sleep(LOGOUT_RETRY_EVERY_S)


if __name__ == "__main__":
    raise SystemExit(main())

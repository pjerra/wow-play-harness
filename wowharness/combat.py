"""1.12 combat-log text to events. Patterns follow the client's GlobalStrings; an unknown line is kept as kind 'other'."""
import re
from dataclasses import dataclass

TIME = re.compile(r"^(\d+/\d+ \d+:\d+:\d+\.\d+)\s{2}(.*)$")
N = r"(.+?)"  # a name: lazy, may hold spaces and apostrophes

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("slain", re.compile(rf"^You have slain {N}!$")),
    ("slain_by", re.compile(rf"^{N} is slain by {N}!$")),
    ("aura", re.compile(rf"^{N} (?:is|are) afflicted by {N}\.$")),
    ("periodic", re.compile(rf"^{N} suffers? (\d+) \w+ damage from (?:your() {N}|{N}'s {N})\.$")),
    ("heal", re.compile(rf"^{N}'s {N} (critically )?heals {N} for (\d+)\.$")),
    ("heal", re.compile(rf"^Your() {N} (critically )?heals {N} for (\d+)\.$")),
    ("spell", re.compile(rf"^{N}'s {N} (hits|crits) {N} for (\d+)(?: \w+ damage)?\.$")),
    ("spell", re.compile(rf"^Your() {N} (hits|crits) {N} for (\d+)(?: \w+ damage)?\.$")),
    ("cast_start", re.compile(rf"^{N} begins? to cast {N}\.$")),
    ("cast", re.compile(rf"^{N} casts? {N} on {N}\.$")),
    ("gain", re.compile(rf"^{N} gains? {N}\.$")),
    ("dies", re.compile(rf"^{N} dies\.$")),
    ("melee", re.compile(rf"^{N} (hits?|crits?) {N} for (\d+)\.$")),
]


@dataclass(frozen=True)
class Event:
    time: str
    kind: str
    source: str
    spell: str | None
    target: str | None
    amount: int | None
    crit: bool
    raw: str


def _who(name: str, me: str) -> str:
    return me if name in ("You", "you", "") else name


def parse_line(line: str, me: str) -> Event:
    m = TIME.match(line)
    t, body = (m.group(1), m.group(2)) if m else ("", line)
    for kind, rx in PATTERNS:
        g = rx.match(body)
        if not g:
            continue
        x = g.groups()
        if kind == "slain":
            return Event(t, kind, me, None, x[0], None, False, line)
        if kind == "slain_by":
            return Event(t, "slain", _who(x[1], me), None, x[0], None, False, line)
        if kind == "aura":  # the combat log does not name the caster
            return Event(t, kind, "", x[1], _who(x[0], me), None, False, line)
        if kind == "periodic":
            target, amount, mine, my_spell, src, spell = x
            if mine is not None:
                src, spell = me, my_spell
            return Event(t, kind, _who(src, me), spell, _who(target, me), int(amount), False, line)
        if kind == "heal":
            return Event(t, kind, _who(x[0], me), x[1], _who(x[3], me), int(x[4]), bool(x[2]), line)
        if kind == "spell":
            return Event(t, kind, _who(x[0], me), x[1], _who(x[3], me), int(x[4]), x[2] == "crits", line)
        if kind == "cast_start":
            return Event(t, kind, _who(x[0], me), x[1], None, None, False, line)
        if kind == "cast":
            return Event(t, kind, _who(x[0], me), x[1], _who(x[2], me), None, False, line)
        if kind == "gain":
            return Event(t, kind, _who(x[0], me), x[1], None, None, False, line)
        if kind == "dies":
            return Event(t, kind, x[0], None, None, None, False, line)
        if kind == "melee":
            return Event(t, kind, _who(x[0], me), None, _who(x[2], me), int(x[3]), x[1].startswith("crit"), line)
    return Event(t, "other", "", None, None, None, False, line)


def parse_lines(lines, me: str) -> list[Event]:
    return [parse_line(l, me) for l in lines if l.strip() and not l.startswith("#")]

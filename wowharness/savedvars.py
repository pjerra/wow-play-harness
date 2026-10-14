"""The observer addon's SavedVariables file (HXDB) as a log: the client writes it on ReloadUI/logout only.

HXDB.<key> is a ring of lines; HXDB.<key>_first is the absolute number of its first line, so an
offset is the absolute number of the next line to read and survives the ring dropping old lines.
"""
import re

_TOKEN = re.compile(r'\s+|--[^\n]*|"((?:[^"\\]|\\.)*)"|(-?\d+(?:\.\d+)?)|(\w+)|(\[|\]|\{|\}|=|,)', re.S)
_ESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}


def _tokens(text: str):
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"unexpected Lua at {pos}: {text[pos:pos + 20]!r}")
        pos = m.end()
        s, num, word, punct = m.groups()
        if s is not None:
            yield ("str", re.sub(r"\\(.)", lambda e: _ESC.get(e.group(1), e.group(1)), s))
        elif num is not None:
            yield ("num", float(num) if "." in num else int(num))
        elif word is not None:
            yield ("word", word)
        elif punct is not None:
            yield ("p", punct)


def _value(toks: list, i: int):
    kind, v = toks[i]
    if kind in ("str", "num"):
        return v, i + 1
    if kind == "word":
        return {"true": True, "false": False, "nil": None}.get(v, v), i + 1
    if v != "{":
        raise ValueError(f"unexpected {v!r}")
    i += 1
    arr, rec = [], {}
    while toks[i] != ("p", "}"):
        if toks[i] == ("p", "["):
            key, i = _value(toks, i + 1)
            i += 2  # ] =
            rec[key], i = _value(toks, i)
        else:
            item, i = _value(toks, i)
            arr.append(item)
        if toks[i] == ("p", ","):
            i += 1
    i += 1
    if arr and rec:
        return {**dict(enumerate(arr, 1)), **rec}, i
    return (rec if rec else arr), i


def parse_lua_assignments(text: str) -> dict:
    """`Name = <value>` lines as WoW writes SavedVariables: strings, numbers, booleans, nil, nested tables."""
    toks, out, i = list(_tokens(text)), {}, 0
    while i < len(toks):
        name = toks[i][1]
        out[name], i = _value(toks, i + 2)
    return out


class SavedLog:
    """Same interface as logs.LogTail, over HXDB[key] in the addon's SavedVariables file."""

    def __init__(self, path: str, key: str):
        self.path, self.key = path, key

    def _ring(self) -> tuple[list[str], int]:
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                db = parse_lua_assignments(f.read()).get("HXDB") or {}
        except FileNotFoundError:
            return [], 1
        lines = db.get(self.key) or []
        if isinstance(lines, dict):
            lines = [lines[k] for k in sorted(k for k in lines if isinstance(k, int))]
        return lines, int(db.get(self.key + "_first", 1))

    def size(self) -> int:
        lines, first = self._ring()
        return first + len(lines)

    def read_since(self, offset: int) -> tuple[list[str], int, bool]:
        lines, first = self._ring()
        end = first + len(lines)
        if offset < first or offset > end:
            return lines, end, True
        return lines[offset - first:], end, False

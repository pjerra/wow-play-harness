"""Headless backend: the tester surface of client.Client / mcp.py over turtle-agent's JSON-RPC.

turtle-agent (github.com/pjerra/turtle-agent) logs one character in without a game window and
serves JSON-RPC 2.0 on a loopback port. Offsets here are event sequence numbers (status.last_seq).
"""
import itertools
import json
import os
import stat
import time
import urllib.error
import urllib.request
from datetime import datetime

from .combat import Event
from .report import build, summary

DEFAULT_RPC = "127.0.0.1:8765"
CHAT_KINDS = ["chat", "whisper", "system"]
COMBAT_KINDS = ["melee", "spell_damage", "periodic", "heal", "energize", "spell_start", "spell_go",
                "spell_miss", "spell_fail", "aura_applied", "aura_removed", "death", "kill"]
WHISPER = ("/w", "/whisper", "/t", "/tell")
NEEDS_REAL_CLIENT = ("/script", "/run", "/hx", "/reload", "/console")
SLASH = {"/s": "say", "/say": "say", "/p": "party", "/party": "party", "/y": "yell", "/yell": "yell",
         "/e": "emote", "/emote": "emote", "/me": "emote", "/g": "guild", "/guild": "guild",
         "/ra": "raid", "/raid": "raid"}
SCHOOLS = ["Physical", "Holy", "Fire", "Nature", "Frost", "Shadow", "Arcane"]
POWERS = ["Mana", "Rage", "Focus", "Energy", "Happiness"]
OUTCOMES = {"dodge": "dodges", "parry": "parries", "block": "blocks", "evade": "evades",
            "immune": "is immune", "deflect": "deflects"}
# Spell misses (data.miss, SpellMissInfo names) as the 1.12 GlobalStrings SPELL*OTHEROTHER lines.
SPELL_MISSES = {"resist": "was resisted by", "dodge": "was dodged by", "parry": "was parried by",
                "block": "was blocked by", "evade": "was evaded by", "deflect": "was deflected by",
                "absorb": "is absorbed by", "reflect": "is reflected back by"}


class HeadlessError(Exception):
    """A turtle-agent RPC error, or something only the real client can do."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def clock(iso: str) -> str:
    """turtle-agent's RFC 3339 time as the chat/combat log prefix M/D HH:MM:SS.mmm (local time)."""
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    return f"{t.month}/{t.day} {t:%H:%M:%S}.{t.microsecond // 1000:03d}"


def _school(n) -> str:
    return SCHOOLS[n] if isinstance(n, int) and 0 <= n < len(SCHOOLS) else "Physical"


def _power(n) -> str:
    return POWERS[n] if isinstance(n, int) and 0 <= n < len(POWERS) else "Mana"


def load_spell_names(path: str) -> dict[int, str]:
    """A JSON file mapping spell id to name ({"133": "Fireball"}), for the report's spell names."""
    with open(path, encoding="utf-8") as f:
        return {int(k): v for k, v in json.load(f).items()}


def combat_line(e: dict, spell_name) -> str | None:
    """One combat event as a 1.12 combat-log line that combat.parse_line understands."""
    k, d = e["kind"], e.get("data") or {}
    src, tgt = e.get("source") or "Unknown", e.get("target") or "Unknown"
    sp = spell_name(e["spell_id"]) if e.get("spell_id") else ""
    amt, hit = e.get("amount", 0), "crits" if e.get("crit") else "hits"
    if k == "melee":
        o = d.get("outcome")
        body = (f"{src} {hit} {tgt} for {amt}." if o == "normal" else
                f"{src} misses {tgt}." if o == "miss" else f"{src} attacks. {tgt} {OUTCOMES.get(o, o)}.")
    elif k == "spell_damage":
        school = d.get("school", 0)
        body = f"{src}'s {sp} {hit} {tgt} for {amt}" + (f" {_school(school)} damage." if school else ".")
    elif k == "periodic":
        tick = d.get("tick")
        if tick == "damage":
            body = f"{tgt} suffers {amt} {_school(d.get('school', 0))} damage from {src}'s {sp}."
        elif tick == "heal":
            body = f"{tgt} gains {amt} health from {src}'s {sp}."
        elif tick == "leech":
            body = f"{src}'s {sp} drains {amt} {_power(d.get('power'))} from {tgt}."
        else:
            body = f"{tgt} gains {amt} {_power(d.get('power'))} from {src}'s {sp}."
    elif k == "heal":
        body = f"{src}'s {sp} {'critically ' if e.get('crit') else ''}heals {tgt} for {amt}."
    elif k == "energize":
        body = f"{tgt} gains {amt} {_power(d.get('power'))} from {src}'s {sp}."
    elif k == "spell_miss":
        m = d.get("miss")
        body = (f"{tgt} resists {src}'s {sp}." if d.get("proc") else
                f"{src}'s {sp} fails. {tgt} is immune." if m == "immune" else
                f"{src}'s {sp} {SPELL_MISSES[m]} {tgt}." if m in SPELL_MISSES else f"{src}'s {sp} misses {tgt}.")
    elif k == "spell_fail":
        # reason: a SpellCastResult code (SpellDefines.h) for the player's own casts, a word for others'.
        r = d.get("reason")
        body = f"{src} fails to cast {sp}: {f'error {r}' if isinstance(r, int) else str(r).capitalize()}."
    elif k == "spell_start":
        body = f"{src} begins to cast {sp}."
    elif k == "spell_go":
        body = f"{src} casts {sp} on {tgt}." if e.get("target") else f"{src} casts {sp}."
    elif k == "aura_applied":
        body = f"{tgt} is afflicted by {sp}." if d.get("harmful") else f"{tgt} gains {sp}."
    elif k == "aura_removed":
        body = f"{sp} fades from {tgt}."
    elif k == "death":
        body = f"{src} dies."
    elif k == "kill":
        body = f"{tgt} is slain by {src}!"
    else:
        return None
    return f"{clock(e['time'])}  {body}"


def chat_line(e: dict) -> str:
    """One chat/system event as a chat-log line."""
    d = e.get("data") or {}
    typ, src, text = d.get("type", e["kind"]), e.get("source") or "", e.get("text", "")
    body = {
        "say": f"[{src}] says: {text}", "yell": f"[{src}] yells: {text}", "party": f"[Party] [{src}]: {text}",
        "whisper": f"[{src}] whispers: {text}", "whisper_inform": f"To [{src}]: {text}", "emote": f"{src} {text}",
        "guild": f"[Guild] [{src}]: {text}", "raid": f"[Raid] [{src}]: {text}", "monster_say": f"{src} says: {text}",
        "monster_yell": f"{src} yells: {text}", "monster_whisper": f"{src} whispers: {text}",
        "channel": f"[{d.get('channel', '')}] [{src}]: {text}", "hardcore": f"[Hardcore] [{src}]: {text}",
    }.get(typ, text)
    return f"{clock(e['time'])}  {body}"


def to_report_events(evs: list[dict], spell_name) -> list[Event]:
    """Structured events as combat.Event, the input report.build expects."""
    effects = {(e.get("source"), e.get("spell_id")) for e in evs if e["kind"] in ("spell_damage", "heal")}
    out = []
    for e in evs:
        k, d = e["kind"], e.get("data") or {}
        t, src, tgt = clock(e["time"]), e.get("source") or "", e.get("target")
        sp = spell_name(e["spell_id"]) if e.get("spell_id") else None
        amt, crit, raw = e.get("amount", 0), bool(e.get("crit")), combat_line(e, spell_name) or ""
        if k == "melee":
            out.append(Event(t, "melee", src, None, tgt, amt, crit, raw) if d.get("outcome") == "normal"
                       else Event(t, "other", "", None, None, None, False, raw))
        elif k == "spell_damage":
            out.append(Event(t, "spell", src, sp, tgt, amt, crit, raw))
        elif k == "periodic" and d.get("tick") in ("damage", "heal"):
            out.append(Event(t, "periodic" if d["tick"] == "damage" else "heal", src, sp, tgt, amt, False, raw))
        elif k == "heal":
            out.append(Event(t, "heal", src, sp, tgt, amt, crit, raw))
        elif k == "spell_start":
            out.append(Event(t, "cast_start", src, sp, None, None, False, raw))
        elif k == "spell_go" and tgt and (src, e.get("spell_id")) not in effects:
            out.append(Event(t, "cast", src, sp, tgt, None, False, raw))
        elif k == "aura_applied":
            out.append(Event(t, "aura", "", sp, tgt, None, False, raw) if d.get("harmful")
                       else Event(t, "gain", tgt or "", sp, None, None, False, raw))
        elif k == "death":
            out.append(Event(t, "dies", src, None, None, None, False, raw))
        elif k == "kill":
            out.append(Event(t, "slain", src, None, tgt, None, False, raw))
        elif k in ("spell_miss", "spell_fail"):  # the real client's parser keeps these lines as 'other' too
            out.append(Event(t, "other", "", None, None, None, False, raw))
    return out


def _pct(cur, mx) -> int:
    return int(cur * 100 // mx) if mx else 0


def rpc_token(rpc: str, folder: str = "~/.config/turtle-agent") -> str:
    """The token turtle-agent wrote for this RPC port (rpc-<port>.token next to its settings), or "".

    Same rules as the agent's own reader: a regular file, not a symlink, not readable by group or others."""
    path = os.path.join(os.path.expanduser(folder), f"rpc-{rpc.rsplit(':', 1)[-1]}.token")
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read(4096).strip()
    except OSError:
        return ""


class HeadlessClient:
    def __init__(self, rpc: str = DEFAULT_RPC, timeout_s: float = 35.0, spell_names: dict[int, str] | None = None,
                 token: str | None = None):
        self.url = f"http://{rpc}/rpc"
        self.rpc = rpc
        self.token = token
        self.timeout_s = timeout_s
        self.spell_names = dict(spell_names or {})
        self._ids = itertools.count(1)

    def _spell(self, spell_id: int) -> str:
        return self.spell_names.get(spell_id, f"spell {spell_id}")

    def call(self, method: str, **params):
        body = json.dumps({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}).encode()
        headers = {"Content-Type": "application/json"}
        token = self.token if self.token is not None else rpc_token(self.rpc)  # re-read: a restarted agent writes a new one
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                out = json.loads(resp.read().decode())
        except urllib.error.HTTPError as err:
            if err.code == 401:
                raise HeadlessError(f"turtle-agent at {self.url} refused the RPC token (401): check rpc-<port>.token "
                                    "next to its settings, or rpc_auth: off") from None
            raise HeadlessError(f"turtle-agent at {self.url}: HTTP {err.code}") from None
        except OSError as err:
            raise HeadlessError(f"turtle-agent not reachable at {self.url}: {err}") from None
        if out.get("error"):
            e = out["error"]
            raise HeadlessError(f"{method}: {e.get('message')} ({e.get('code')})", e.get("code"))
        return out.get("result")

    def _read_all(self, since: int, kinds: list[str]) -> tuple[list[dict], int, bool]:
        evs, dropped = [], False
        while True:
            page = self.call("events", since=since, kinds=kinds, limit=5000)
            evs += page["events"]
            dropped = dropped or page["dropped"]
            since = page["next"]
            if not page["more"]:
                return evs, since, dropped

    def _collect(self, since: int, kinds: list[str], wait_s: float, until=None) -> list[dict]:
        """Events of these kinds seen within wait_s (read at least once, so wait_s 0 is a single check);
        stops early once until(events so far) is true."""
        out, deadline = [], time.monotonic() + wait_s
        while True:
            left = max(deadline - time.monotonic(), 0)
            page = self.call("events", since=since, kinds=kinds, wait_ms=int(left * 1000))
            out += page["events"]
            since = page["next"]
            if (until and until(out)) or deadline - time.monotonic() <= 0:
                return out
            if not page["events"]:
                time.sleep(min(0.05, max(deadline - time.monotonic(), 0)))

    def chat(self, text: str) -> None:
        """A chat-box line, as Client.chat takes it: "." lines are commands, /s /p /y /e /g /ra /w pick the
        channel, plain text is say. Lua (/script, /run) and /hx need the real client."""
        if len(text) > 255:
            raise ValueError(f"chat line is {len(text)} chars; WoW cuts at 255 - use an HX_* addon helper or split it")
        t = text.strip()
        if t.lower().startswith(NEEDS_REAL_CLIENT):
            raise HeadlessError(f"{t.split()[0]} needs the real client (no Lua in headless mode)")
        if t.startswith("."):
            self.call("gm", text=t)
            return
        if t.startswith("/"):
            cmd, _, rest = t.partition(" ")
            cmd = cmd.lower()
            if cmd in SLASH or cmd in WHISPER:
                if not rest.strip() or (cmd in WHISPER and not rest.partition(" ")[2].strip()):
                    raise ValueError(f"{cmd} needs text" + (" after the name" if cmd in WHISPER else ""))
            if cmd in WHISPER:
                to, _, msg = rest.partition(" ")
                self.call("chat", type="whisper", to=to, text=msg)
                return
            if cmd not in SLASH:
                raise HeadlessError(f"unsupported slash command {cmd} in headless mode")
            self.call("chat", type=SLASH[cmd], text=rest)
            return
        self.call("chat", type="say", text=t)

    def gm(self, cmd: str, wait_s: float = 3.0) -> dict:
        """A GM command run by the character; the reply is the system lines that followed within wait_s."""
        since = self.call("status")["last_seq"]
        self.call("gm", text=cmd)
        return {"ok": True, "reply": "\n".join(e.get("text", "") for e in self._collect(since, ["system"], wait_s))}

    def bot(self, cmd: str, wait_s: float = 5.0) -> dict:
        """.bot <cmd>; returns the chat lines seen within wait_s (TBM:/bot replies)."""
        since = self.call("status")["last_seq"]
        self.call("gm", text=".bot " + cmd)
        return {"lines": [chat_line(e) for e in self._collect(since, CHAT_KINDS, wait_s)]}

    def live_offset(self) -> int:
        """Where the event stream is now (status.last_seq), for live_lines/wait_live."""
        return self.call("status")["last_seq"]

    def _render(self, e: dict) -> str | None:
        return chat_line(e) if e["kind"] in CHAT_KINDS else combat_line(e, self._spell)

    @staticmethod
    def _live_kinds(kind: str | None) -> list[str]:
        """The event kinds behind a live kind (None = chat and combat); empty for an unknown kind."""
        return (COMBAT_KINDS if kind in (None, "combat") else []) + (CHAT_KINDS if kind in (None, "chat") else [])

    def live_lines(self, since: int, kind: str | None = None) -> tuple[list[str], int]:
        """Lines (chat, combat or both) seen since a live offset, and the next offset. Both kinds come from
        one read, so an event that lands between two reads is never skipped by the next offset."""
        kinds = self._live_kinds(kind)
        if not kinds:
            return [], since
        evs, nxt, _ = self._read_all(since, kinds)
        return [l for e in evs if (l := self._render(e))], max(nxt, since)

    def wait_live(self, pattern: str, since: int, timeout_s: float, kind: str | None = None) -> str | None:
        """The first line matching the regex pattern that appears after since, or None on timeout
        (timeout 0 checks once; an unknown kind returns None at once)."""
        import re
        rx = re.compile(pattern)
        kinds = self._live_kinds(kind)
        if not kinds:
            return None
        found = []

        def hit(evs):
            found[:] = [l for e in evs if (l := self._render(e)) and rx.search(l)]
            return bool(found)

        self._collect(since, kinds, timeout_s, hit)
        return found[0] if found else None

    def run_lua(self, code: str) -> None:
        raise HeadlessError("run_lua needs the real client (no Lua in headless mode)")

    def key(self, name: str, hold_ms: int = 0) -> None:
        raise HeadlessError("key needs the real client (no game window in headless mode)")

    def state(self) -> dict:
        """The observer addon's HX| state line, built from status, target and party."""
        st = self.call("status")
        line = (f"HX|p={st.get('character', '?')},{_pct(st.get('health', 0), st.get('max_health', 0))},"
                f"{_pct(st.get('power', 0), st.get('max_power', 0))},{st.get('x', 0.0):.1f},{st.get('y', 0.0):.1f},"
                f"map{st.get('map', 0)}")
        if st.get("target"):
            for u in self.call("units", limit=200):
                if u["guid"] == st["target"]:
                    line += f"|t={u['name']},{_pct(u['health'], u['max_health'])},{'dead' if u.get('dead') else 'alive'}"
        members = self.call("party").get("members") or []
        if members:
            line += "|party=" + ",".join(f"{m['name']}:{_pct(m['health'], m['max_health'])}" for m in members)
        return {"line": line, "via": "headless", "status": st}

    def tail_combat_log(self, since: int = -1) -> dict:
        if since < 0:
            return {"offset": self.call("status")["last_seq"], "lines": []}
        evs, nxt, dropped = self._read_all(since, COMBAT_KINDS)
        return {"offset": nxt, "lines": [l for l in (combat_line(e, self._spell) for e in evs) if l], "restarted": dropped}

    def tail_chat(self, since: int = -1) -> dict:
        if since < 0:
            return {"offset": self.call("status")["last_seq"], "lines": []}
        evs, nxt, dropped = self._read_all(since, CHAT_KINDS)
        return {"offset": nxt, "lines": [chat_line(e) for e in evs], "restarted": dropped}

    def fight_report(self, combat_since: int, chat_since: int, me: str, party: list[str]) -> dict:
        evs, _, _ = self._read_all(combat_since, COMBAT_KINDS)
        chat, _, _ = self._read_all(chat_since, CHAT_KINDS)
        rep = build(to_report_events(evs, self._spell), [chat_line(e) for e in chat], me, party)
        return {"report": rep, "summary": summary(rep, party)}

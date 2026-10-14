"""A fake turtle-agent and a fake bot dashboard on a shared virtual clock, for the build gate tests.

FakeWorld answers the JSON-RPC methods the gate uses and serves the dashboard's bot list; both run over
real HTTP (FakeServers) so HeadlessClient and Dashboard are exercised as in a live run. Time only moves
when the gate sleeps (clock.sleep) or walks (walk_to advances the clock by distance / run speed).
`bugs` switches on tonight's failures: no_cross_map_follow (build 11), stay_sticks (build 13),
healer_stall (repro 129), caster_stall (repro 124), late_sheep (b11-focus), jump (#125),
caster_casts_from_range (the caster_stall stand, still casting every 1.5 s),
logout_refuse_once / logout_refuse; reply_lag / reply_none (a stay reply that reaches the log late / never);
for the moon scenario mage_eats (sits down to eat at the summon),
mage_eats_forever, mage_eats_after_order, moon_joins_late (Moon mob enters combat 8 s after the pull,
sheep 12 s after the pull), moon_never_fights; dead_lag (the units list shows a creature alive for 1.5 s after it
dies), respawn_lag (`.respawn` shows 5 s later) / respawn_never;
ack_lag (the cc ACK reaches the log 5 s late); `world.cc_race = n` makes the first n cc orders run on the selection from before the latest `target`
(the selection packet and the chat command are separate server queues), and every cc order moves the icon (raid_mark `cleared`/`set` rows, none when the holder is unchanged; `world.mark_delay` makes them late).
A cc order on a dead or unknown selection is refused (TBM:ACTION_ERR|cc <mark> <mage>|no-target|..., no mark
row, no assignment); cc_no_cc sets the icon and then refuses the order (TBM:ACTION_ERR|cc <mark> <mage>|no-cc|...).
Replies use the server's exact shapes (BotCommands.cpp SendActionAck / SendActionError): the ACK is
TBM:ACTION_ACK|cc <mark> <mage>|bot:<mage>|1|<mage>.
Setup switches (the gate's ready checks): slow_leader (a
cross-map teleport takes 4 s: the leader's position walks to the spot), summon_lost_once (the first .bot summon of
each bot never lands), attack_refused (`.bot action attack` is refused: the target is evading). `world.stuck` holds
mobs that keep the party in combat until they die (each targets a party bot, as a creature's UNIT_FIELD_TARGET is
its victim).

`units` lists what turtle-agent's real `units` lists: creatures (guid high 0xF130, `hostile` unless they have NPC
flags), the party bots as player rows (`type` player, `in_party`, in combat while they fight) and `world.others`
(pets 0xF140, friendly NPCs, other players: add_other). `world.died` and `world.attack_orders` record the selection
each GM `.die` and `.bot action attack` was sent on, whatever it was."""
import json
import math
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RUN = 7.0
POLY_ID = 12826
TELE = {"ulda": (70, -226.9, 54.1, -45.5), "badlands": (0, -6778.0, -3425.0, 241.0)}
CLASS_ROLE = {"warrior": "tank", "priest": "healer", "mage": "dps", "rogue": "dps"}
NAMES = {"warrior": "Latal", "priest": "Niabrenans", "mage": "Maethiza", "rogue": "Kaeporric"}
ENTRY_NAMES = {4851: "Stonevault Rockchewer", 4852: "Stonevault Oracle"}
# Uldaman's west entrance pack (gameserver, fresh instance, 2026-10-12)
PACK_A = [(4851, -279.1, 60.3, -48.3, 35), (4852, -275.4, 62.2, -48.3, 59),  # Rockchewers are friendly (faction 35) on gameserver now
          (4852, -173.3, 73.3, -48.3, 59), (4852, -164.2, 68.2, -48.3, 59)]       # the Hall of the Keepers Oracles


class Clock:
    def __init__(self, t: float = 1_791_810_000.0):
        self.t = t
        self.world = None

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        if self.world:
            self.world.advance(s)
        else:
            self.t += s


class RpcError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


class Bot:
    def __init__(self, name, cls):
        self.name, self.cls, self.role = name, cls, CLASS_ROLE[cls]
        self.guid = ""  # a player guid, set at hire
        self.map, self.x, self.y, self.z = 0, 0.0, 0.0, 0.0
        self.hp = self.max_hp = 2000
        self.mode = "follow"
        self.combat_until = 0.0
        self.eat_until = 0.0  # eating or drinking until then (hp/mana down, state resting)
        self.action, self.trigger, self.state = "check values", "very often", "idle"
        self.arrive_at = None
        self.fight_pos = None  # where the bot stands to fight (melee at the mob, casters 25 yd off)

    def snapshot(self, now):
        combat = now < self.combat_until
        eating = now < self.eat_until and not combat
        strat = (["dps", "frost", "aoe"] if combat else
                 ["chat", "loot"] + (["follow", "follow jump"] if self.mode == "follow" else ["stay"]))
        snap = {"name": self.name, "class": self.cls, "role": self.role, "map": self.map, "x": round(self.x, 2),
                "y": round(self.y, 2), "z": self.z, "state": "combat" if combat else self.state,
                "last_action": self.action, "last_trigger": self.trigger, "strategy": ", ".join(strat),
                "hp": int(self.hp * 0.6) if eating else self.hp, "max_hp": self.max_hp,
                "power": 600 if eating else 1000, "max_power": 1000, "power_type": "mana", "target": ""}
        if eating:
            snap.update(state="resting", last_action="drink")
        return snap


class FakeWorld:
    def __init__(self, clock: Clock, bugs=(), start=(0, -6070.0, -2956.0, 210.0)):
        self.clock, self.bugs = clock, set(bugs)
        clock.world = self
        self.lock = threading.RLock()
        self.leader = {"name": "Harnessb", "guid": "0x0000000000000010", "map": start[0], "x": start[1], "y": start[2], "z": start[3],
                       "health": 3000, "max_health": 3000, "target": "", "state": "in_world"}
        self.gm = False
        self.ate = False
        self.eating_at_order = self.eating_at_pull = None  # who was eating at the cc order / at the pull
        self.bots: dict[str, Bot] = {}
        self.party: list[str] = []
        self.creatures: dict[str, dict] = {}
        self.events: list[dict] = []
        self.timers: list[tuple[float, object]] = []
        self.calls: list[tuple[str, dict]] = []
        self.cc = {}  # mark -> (owner, guid)
        self.cc_race = 0  # cc orders still to run on the selection from before the latest `target`
        self.sel_before = ""
        self.marks: dict[str, str] = {}  # icon -> the guid that holds it (`cc clear` resets the bot's rti, not the icon)
        self.mark_delay = 0.0  # seconds the raid_mark event of a cc order takes to show
        self.cc_race_guid = None  # the selection those orders run on (default: the one from before the latest `target`)
        self.logout_refusals = 0
        self.next_guid = 0x100
        self.next_player = 0x20
        self.others: dict[str, dict] = {}  # non-party units in view: pets, friendly NPCs, other players (add_other)
        self.died: list = []  # the selection at each GM .die
        self.attack_orders: list = []  # the selection at each .bot action attack
        self.stuck: set[str] = set()  # mobs the party keeps fighting until they die
        self.path = None  # slow_leader: the leader's walk from the old spot to the new map's spot
        self.summon_sent: dict[str, int] = {}
        self.summons: list[tuple[float, bool]] = []  # (time, the leader was still arriving) per .bot summon
        for e, x, y, z, f in PACK_A:
            self._spawn(e, 70, x, y, z, f)

    def _spawn(self, e, m, x, y, z, faction=16):
        g = f"0xF1300{e:05X}{self.next_guid:06X}"
        self.next_guid += 1
        self.creatures[g] = {"entry": e, "name": ENTRY_NAMES.get(e, f"npc {e}"), "map": m, "x": x, "y": y, "z": z,
                             "hp": 1500, "max_hp": 1500, "dead": False, "in_combat": False, "faction": faction,
                             "target": None}
        return g

    def add_other(self, kind, name, x, y, z=None, in_combat=True, target=None, entry=0, m=None, hostile=False):
        """A non-party unit at x, y on the leader's map, as turtle-agent lists it: kind "pet" (guid high 0xF140,
        player-controlled so not hostile), "npc" (a 0xF130 creature with NPC flags: a vendor or guard, not hostile) or
        "player" (another player). Returns its guid."""
        n = self.next_guid
        self.next_guid += 1
        g = {"pet": f"0xF14000{n:010X}", "npc": f"0xF1300{entry or 3000:05X}{n:06X}", "player": f"0x{n:016X}"}[kind]
        self.others[g] = {"type": "player" if kind == "player" else "creature", "name": name,
                          "map": self.leader["map"] if m is None else m, "x": x, "y": y,
                          "z": self.leader["z"] if z is None else z, "in_combat": in_combat,
                          "target": target, "dead": False, "hostile": hostile, "in_party": False,
                          "npc_flags": 128 if kind == "npc" else 0, "entry": entry if kind == "npc" else 0}
        return g

    # -- time and events
    def now(self):
        return self.clock.t

    def emit(self, kind, **kw):
        t = datetime.fromtimestamp(self.now(), timezone.utc).isoformat().replace("+00:00", "Z")
        self.events.append({"seq": len(self.events) + 1, "time": t, "kind": kind, **kw})

    def at(self, delay, fn):
        self.timers.append((self.now() + delay, fn))

    def advance(self, dt):
        with self.lock:
            end = self.clock.t + dt
            while self.clock.t < end - 1e-9:
                step = min(0.25, end - self.clock.t)
                self.clock.t += step
                self._tick(step)

    def _tick(self, dt):
        due = sorted([x for x in self.timers if x[0] <= self.now()], key=lambda x: x[0])
        self.timers = [x for x in self.timers if x[0] > self.now()]
        for _, fn in due:
            fn()
        if self.path:
            p = self.path
            f = min(1.0, (self.now() - p["t0"]) / (p["t1"] - p["t0"]))
            self.leader["x"], self.leader["y"], self.leader["z"] = [a + (c - a) * f for a, c in zip(p["from"], p["to"])]
            if f >= 1:
                self.path = None
        for g in list(self.stuck):
            c = self.creatures[g]
            if c["dead"]:
                self.stuck.discard(g)
                continue
            c["in_combat"] = True
            for n in self.party:
                b = self.bots[n]
                if b.map == c["map"] and math.hypot(b.x - c["x"], b.y - c["y"]) <= 40:
                    b.combat_until = max(b.combat_until, self.now() + 1.0)
                    c["target"] = c.get("target") or b.guid
        L = self.leader
        for b in self.bots.values():
            if b.hp <= 0:
                b.state = "dead"
                continue
            if self.now() < b.combat_until:
                if b.fight_pos:
                    self._move(b, b.fight_pos, dt)
                continue
            if b.map != L["map"]:
                if b.mode == "follow" and b.arrive_at is not None and self.now() >= b.arrive_at:
                    b.map, b.x, b.y, b.z, b.arrive_at = L["map"], L["x"] + 2, L["y"] + 1, L["z"], None
                b.state, b.action = "idle", "check values"
                continue
            d = math.hypot(b.x - L["x"], b.y - L["y"])
            if b.mode != "follow" or d <= 3:
                b.state, b.action, b.trigger = "idle", "check values", "very often"
                continue
            if "healer_stall" in self.bugs and b.role == "healer" and d > 10:
                b.state, b.action, b.trigger = "moving", "reach party member to heal", "party member to heal out of spell range"
                continue
            if d > 70:
                b.x, b.y, b.z = L["x"] + 2, L["y"] + 1, L["z"]
                b.action, b.trigger = "follow", "out of free move range"
                continue
            slow = "healer_stall" in self.bugs and b.role == "healer"  # lags behind, then freezes past 10 yd
            self._move(b, {"x": L["x"], "y": L["y"]}, dt * (0.5 if slow else 1), stop=2.5)
            b.state, b.trigger = "moving", "out of free move range"
            b.action = "jump" if ("jump" in self.bugs and b.map == 70 and b.role == "dps" and b.cls == "rogue") else "follow"

    @staticmethod
    def _move(b, to, dt, stop=0.5):
        d = math.hypot(to["x"] - b.x, to["y"] - b.y)
        if d <= stop:
            return
        f = min(RUN * dt, d - stop) / d
        b.x += (to["x"] - b.x) * f
        b.y += (to["y"] - b.y) * f

    # -- RPC
    def handle(self, method, p):
        with self.lock:
            self.calls.append((method, p))
            if self.leader["state"] != "in_world" and method != "status":
                raise RpcError(-32001, "not in world")
            fn = getattr(self, "rpc_" + method, None)
            if not fn:
                raise RpcError(-32601, "Method not found: " + method)
            return fn(p)

    def rpc_status(self, p):
        L = self.leader
        return {"state": L["state"], "character": L["name"], "guid": L["guid"], "map": L["map"], "x": L["x"], "y": L["y"], "z": L["z"],
                "health": L["health"], "max_health": L["max_health"], "target": L["target"], "last_seq": len(self.events)}

    def rpc_events(self, p):
        kinds = p.get("kinds") or []
        evs = [e for e in self.events if e["seq"] > p.get("since", 0) and (not kinds or e["kind"] in kinds)]
        return {"events": evs, "next": len(self.events), "oldest": 1, "dropped": False, "more": False}

    def rpc_party(self, p):
        return {"in_group": bool(self.party), "members": [{"name": n, "guid": self.bots[n].guid, "health": self.bots[n].hp,
                                                            "max_health": self.bots[n].max_hp} for n in self.party]}

    def rpc_units(self, p):
        L, out = self.leader, []
        for g, c in self.creatures.items():
            if c["map"] != L["map"]:
                continue
            d = math.hypot(c["x"] - L["x"], c["y"] - L["y"])
            if p.get("near") and d > p["near"] or p.get("entry") and c["entry"] != p["entry"]:
                continue
            if p.get("name") and p["name"].lower() not in c["name"].lower():
                continue
            stale = ("dead_lag" in self.bugs and c.get("killed_at") is not None
                     and self.now() - c["killed_at"] < 1.5)  # the list still shows the creature as it was alive
            dead = c["dead"] and not stale
            row = {"guid": g, "type": "creature", "entry": c["entry"], "name": c["name"], "x": c["x"], "y": c["y"],
                   "z": c["z"], "distance": d, "health": c["max_hp"] if stale else c["hp"], "max_health": c["max_hp"],
                   "in_combat": c["in_combat"], "dead": dead, "hostile": not dead, "in_party": False,
                   "npc_flags": 0}
            if c.get("target"):
                row["target"] = c["target"]
            out.append(row)
        if not p.get("entry"):  # players and pets have no entry; an entry filter never matches them
            now = self.now()
            for b in self.bots.values():  # the party bots: player rows, in combat while they fight
                if b.map != L["map"] or b.name not in self.party:
                    continue
                d = math.hypot(b.x - L["x"], b.y - L["y"])
                if p.get("near") and d > p["near"] or p.get("name") and p["name"].lower() not in b.name.lower():
                    continue
                out.append({"guid": b.guid, "type": "player", "name": b.name, "x": b.x, "y": b.y, "z": b.z,
                            "distance": d, "health": b.hp, "max_health": b.max_hp, "in_combat": now < b.combat_until,
                            "dead": b.hp <= 0, "hostile": False, "in_party": True, "npc_flags": 0})
            for g, o in self.others.items():
                if o["map"] != L["map"]:
                    continue
                d = math.hypot(o["x"] - L["x"], o["y"] - L["y"])
                if p.get("near") and d > p["near"] or p.get("name") and p["name"].lower() not in o["name"].lower():
                    continue
                row = {k: v for k, v in o.items() if k not in ("map", "target", "entry")}
                row.update(guid=g, distance=d, health=0 if o["dead"] else 1000, max_health=1000)
                if o.get("target"):
                    row["target"] = o["target"]
                out.append(row)
        elif any(o.get("entry") == p["entry"] for o in self.others.values()):
            for g, o in self.others.items():
                if o.get("entry") == p["entry"] and o["map"] == L["map"]:
                    d = math.hypot(o["x"] - L["x"], o["y"] - L["y"])
                    if not (p.get("near") and d > p["near"]):
                        row = {k: v for k, v in o.items() if k not in ("map", "target")}
                        row.update(guid=g, distance=d, health=0 if o["dead"] else 1000, max_health=1000)
                        if o.get("target"):
                            row["target"] = o["target"]
                        out.append(row)
        return sorted(out, key=lambda u: u["distance"])[: p.get("limit") or 50]

    def rpc_target(self, p):
        g = p.get("guid")
        self.sel_before = self.leader["target"]  # what the server still has when a chat command beats this packet
        self.leader["target"] = g  # like turtle-agent: the selection is sent whether or not the unit is known
        c = self.creatures.get(g) or {"name": "", "hp": 0, "max_hp": 0}
        return {"guid": g, "name": c["name"], "health": c["hp"], "max_health": c["max_hp"]}

    def rpc_stop_attack(self, p):
        return {}

    def rpc_leave_group(self, p):
        if not self.party:
            raise RpcError(-32003, "not in a group")
        for n in self.party:  # leaving dismisses hired companions
            self.bots.pop(n, None)
        self.party = []
        return {}

    def rpc_logout(self, p):
        if "logout_eof" in self.bugs:
            self.leader["state"] = "disconnected"
            raise RpcError(-32002, "logout: disconnected by the server: EOF")
        if "logout_refuse" in self.bugs or ("logout_refuse_once" in self.bugs and self.logout_refusals == 0):
            self.logout_refusals += 1
            raise RpcError(-32003, "logout refused: in combat (reason 1)")
        self.leader["state"] = "logged_out"
        return {}

    def rpc_chat(self, p):
        if p.get("type") == "whisper":
            b = self.bots.get(p["to"])
            if b and p["text"] == "stay":
                b.mode = "stay"
                self._reply(b.name, "Staying")
            elif b and p["text"] == "follow":
                b.mode = "follow"
                self._reply(b.name, "Following")
        return {}

    def _reply(self, source, text):
        """A bot's whisper reply. reply_lag: the reply can arrive more than 4 s after the order; here it reaches
        the log 4.5 s after the order.
        reply_none: the bot never answers."""
        if "reply_none" in self.bugs:
            return
        if "reply_lag" in self.bugs:
            self.at(4.5, lambda: self.emit("whisper", source=source, text=text, data={"type": "whisper"}))
        else:
            self.emit("whisper", source=source, text=text, data={"type": "whisper"})

    def rpc_teleport(self, p):
        self._teleport(p.get("map", self.leader["map"]), p["x"], p["y"], p["z"])
        return {}

    def rpc_walk_to(self, p):
        L = self.leader
        d = math.dist((L["x"], L["y"], L["z"]), (p["x"], p["y"], p["z"]))
        if d > 40:
            raise RpcError(-32004, f"walk_to is for short moves: {d:.1f} yards is over 40, use teleport")
        x0, y0, z0, dur = L["x"], L["y"], L["z"], d / RUN
        steps = max(1, math.ceil(dur / 0.25))
        for i in range(1, steps + 1):  # the leader moves while the world runs
            f = i / steps
            L["x"], L["y"], L["z"] = x0 + (p["x"] - x0) * f, y0 + (p["y"] - y0) * f, z0 + (p["z"] - z0) * f
            self.advance(dur / steps)
        return {}

    def rpc_gm(self, p):
        t = p["text"].strip()
        w = t.split()
        if t == ".gm on" or t == ".gm off":
            self.gm = t == ".gm on"
            self.emit("system", text=f"GM mode is {'ON' if self.gm else 'OFF'}", data={"type": "system"})
        elif w[0] == ".tele":
            m, x, y, z = TELE[w[1].lower()]
            self._teleport(m, x, y, z)
        elif w[:2] == [".bot", "hire"]:
            name = NAMES[w[2]]
            b = Bot(name, w[2])
            b.guid = f"0x{self.next_player:016X}"
            self.next_player += 1
            b.map, b.x, b.y, b.z = self.leader["map"], self.leader["x"] + 1, self.leader["y"] + 1, self.leader["z"]
            self.bots[name] = b
            self.party.append(name)
            self.emit("system", text=f"{name} joins the party.", data={"type": "system"})
        elif w[:2] == [".bot", "summon"]:
            b = self.bots[w[2]]
            self.summons.append((self.now(), self.path is not None))
            self.summon_sent[b.name] = self.summon_sent.get(b.name, 0) + 1
            if self.now() < b.combat_until:  # the module refuses a summon in combat and says so
                self.emit("system", text=f"Bot '{b.name}' is in combat and cannot be summoned.", data={"type": "system"})
                return
            if "summon_lost_once" in self.bugs and self.summon_sent[b.name] == 1:
                return  # the first summon never lands
            b.map, b.x, b.y, b.z, b.arrive_at = self.leader["map"], self.leader["x"] + 2, self.leader["y"], self.leader["z"], None
            if b.cls == "mage" and "mage_eats" in self.bugs and not self.ate:
                self.ate, b.eat_until = True, self.now() + 20  # buffed, sat down to eat: done in 20 s
            if b.cls == "mage" and "mage_eats_forever" in self.bugs:
                b.eat_until = self.now() + 10_000
        elif w[0] == ".revive":
            self.bots[w[1]].hp = self.bots[w[1]].max_hp
        elif w[:2] == [".npc", "summon"]:  # the template's faction: Rockchewers are friendly (35) on gameserver now
            L = self.leader
            self._spawn(int(w[2]), L["map"], L["x"], L["y"], L["z"], 35 if int(w[2]) == 4851 else 59)
        elif w[0] == ".respawn":
            if "respawn_never" in self.bugs:
                return {}
            if "respawn_lag" in self.bugs:
                sel = self.creatures.get(self.leader["target"])
                self.at(5, lambda: self._respawn(sel))
            else:
                self._respawn(self.creatures.get(self.leader["target"]))
        elif w[:2] == [".modify", "hp"]:
            c = self.creatures.get(self.leader["target"])
            if not c:
                raise RpcError(-32005, "modify hp without a creature selected")
            c["hp"] = c["max_hp"] = int(w[2])
        elif w[0] == ".die":
            self.died.append(self.leader["target"])
            o = self.others.get(self.leader["target"])
            if o:  # a pet, a friendly NPC or another player: GM .die kills it all the same
                o.update(dead=True, in_combat=False)
                return {}
            c = self.creatures.get(self.leader["target"])
            if not c:
                raise RpcError(-32005, ".die with no creature selected would kill the player")
            c["dead"], c["hp"], c["in_combat"], c["target"], c["killed_at"] = True, 0, False, None, self.now()
        elif w[:3] == [".bot", "action", "stay"]:
            for n in self.party:
                self.bots[n].mode = "hold"
        elif w[:3] == [".bot", "action", "follow"]:
            for n in self.party:
                self.bots[n].mode = "follow"
        elif w[:3] == [".bot", "action", "cc"] and w[3] == "clear":
            self.cc = {}
        elif w[:3] == [".bot", "action", "cc"]:
            g = self.leader["target"]
            if self.cc_race > 0:  # the chat command ran before the selection packet
                self.cc_race -= 1
                g = self.cc_race_guid or self.sel_before
            c = self.creatures.get(g)
            if c is None or c["dead"]:  # TortoiseBots #841: no live enemy selected, nothing marked, nothing saved
                self.emit("system", text=f"TBM:ACTION_ERR|cc {w[3]} {w[4]}|no-target|Select an enemy target or an owned bot"
                          " with an existing raid mark.", data={"type": "system"})
                return {}
            if "cc_no_cc" in self.bugs:  # the icon is set before the executor check, then the order is refused
                self._set_mark(w[3], g)
                self.emit("system", text=f"TBM:ACTION_ERR|cc {w[3]} {w[4]}|no-cc|No owned bot can CC this target (check class, target type, and spell state).",
                          data={"type": "system"})
                return {}
            self.cc[w[3]] = (w[4], g)
            if self.mark_delay:  # the server's raid target update reaches the log late
                self.at(self.mark_delay, lambda m=w[3], g=g: self._set_mark(m, g))
            else:
                self._set_mark(w[3], g)
            self.eating_at_order = [n for n, b in self.bots.items() if self.now() < b.eat_until]
            if "mage_eats_after_order" in self.bugs:  # the buff pass starts a new meal between order and pull
                self.bots[w[4]].eat_until = self.now() + 4
            ack = lambda: self.emit("system", text=f"TBM:ACTION_ACK|cc {w[3]} {w[4]}|bot:{w[4]}|1|{w[4]}", data={"type": "system"})
            if "ack_lag" in self.bugs:
                self.at(5, ack)
            else:
                ack()
        elif w[:3] == [".bot", "action", "pull"]:
            self._pull(self.leader["target"])
        elif w[:3] == [".bot", "action", "attack"]:
            self.attack_orders.append(self.leader["target"])
            self._action_attack()
        elif w[:2] == [".bot", "attack"]:
            self._attack(w[2], self.leader["target"])
        return {}

    def _set_mark(self, icon, g):
        """The group's icon moves to g; like turtle-agent: a `cleared` row for the unit that lost it, a `set` row for the
        one that got it, and nothing at all when the icon's holder did not change."""
        old = self.marks.get(icon)
        if old == g:
            return
        self.marks[icon] = g
        if old:
            self.emit("raid_mark", target=(self.creatures.get(old) or {}).get("name", ""), target_guid=old,
                      data={"action": "cleared", "icon": icon, "icon_id": 4})
        self.emit("raid_mark", target=(self.creatures.get(g) or {}).get("name", ""), target_guid=g,
                  data={"action": "set", "icon": icon, "icon_id": 4})

    def _respawn(self, sel):
        if sel is None:  # nothing (existing) selected: every dead creature in range
            for c in self.creatures.values():
                if c["dead"] and c["map"] == self.leader["map"] and \
                        math.hypot(c["x"] - self.leader["x"], c["y"] - self.leader["y"]) < 100:
                    c.update(dead=False, hp=c["max_hp"], in_combat=False, killed_at=None)
        elif sel["dead"]:
            sel.update(dead=False, hp=sel["max_hp"], in_combat=False, killed_at=None)

    def _teleport(self, m, x, y, z):
        L = self.leader
        far = m != L["map"]
        if far and "slow_leader" in self.bugs:  # the new map loads over 4 s: the position walks to the spot
            self.path = {"from": (L["x"], L["y"], L["z"]), "to": (x, y, z), "t0": self.now(), "t1": self.now() + 4}
            L["map"] = m
        else:
            self.path = None
            L.update(map=m, x=x, y=y, z=z)
        if far:
            self.emit("session", data={"state": "new_world", "map": m, "x": x, "y": y, "z": z})
            for i, n in enumerate(self.party):
                b = self.bots[n]
                if b.mode == "follow" and ("no_cross_map_follow" not in self.bugs or i == 3):
                    b.arrive_at = self.now() + 3
        else:
            self.emit("session", data={"state": "teleported", "x": x, "y": y, "z": z})

    # -- fights
    def _hit(self, src, guid, amount=80):
        c = self.creatures[guid]
        if c["dead"]:
            return
        self.emit("melee", source=src, target=c["name"], target_guid=guid, amount=amount, party=True,
                  data={"outcome": "normal"})

    def _cast_landed(self, src, guid):
        """src casts a spell that goes off and lands on the mob: spell_start, spell_go and the spell's damage."""
        c = self.creatures[guid]
        self.emit("spell_start", source=src, target=c["name"], target_guid=guid, party=True)
        self.emit("spell_go", source=src, target=c["name"], target_guid=guid, party=True)
        self.emit("spell_damage", source=src, target=c["name"], target_guid=guid, amount=300, party=True)

    def _kill(self, guids):
        for g in guids:
            c = self.creatures[g]
            if not c["dead"]:
                c["dead"], c["hp"], c["in_combat"], c["target"], c["killed_at"] = True, 0, False, None, self.now()
                self.emit("death", source=c["name"], source_guid=g)

    def _action_attack(self):
        """`.bot action attack`: the party engages the selected target; an evading creature is refused."""
        g = self.leader["target"]
        c = self.creatures.get(g)
        if c is None or c["dead"]:
            return
        if "attack_refused" in self.bugs:
            self.emit("system", text=f"TBM:ACTION_ERR|attack|{c['name']} is evading", data={"type": "system"})
            return
        self.emit("system", text="TBM:ACTION_ACK|attack", data={"type": "system"})
        for n in self.party:
            b = self.bots[n]
            if b.role != "healer":
                b.combat_until = max(b.combat_until, self.now() + 6)
        self.at(2, lambda: self._kill([g]))

    def _attack(self, name, guid):
        b, c = self.bots[name], self.creatures[guid]
        if b.role == "tank" and c.get("faction") != 35:  # the tank engages: the pack and the party join in
            self.emit("system", text=f"Bot {name} will attack your selected target.", data={"type": "system"})
            return self._pull(guid)
        if c.get("faction") == 35:
            self.emit("whisper", source=name, text=f"{c['name']} is friendly to me", data={"type": "whisper"})
            return
        c["in_combat"], c["target"] = True, b.guid
        b.combat_until = self.now() + 6
        b.fight_pos = {"x": c["x"], "y": c["y"]}
        self.at(1, lambda: self._hit(name, guid))
        self.at(3, lambda: self._hit(name, guid))
        self.at(5.5, lambda: self._kill([guid]))
        if b.mode == "stay" and "stay_sticks" not in self.bugs:
            self.at(6.1, lambda: setattr(b, "mode", "follow"))

    def _pull(self, guid):
        self.eating_at_pull = [n for n, b in self.bots.items() if self.now() < b.eat_until]
        c = self.creatures[guid]
        pack = [g for g, x in self.creatures.items() if not x["dead"] and x.get("faction") != 35
                and math.hypot(x["x"] - c["x"], x["y"] - c["y"]) < 11]
        tank_guid = next((self.bots[n].guid for n in self.party if self.bots[n].role == "tank"), None)
        for g in pack:
            self.creatures[g].update(in_combat=True, target=tank_guid)
        moon = self.cc.get("moon")
        late = "late_sheep" in self.bugs
        length = 30 if late else 14
        for n in self.party:
            b = self.bots[n]
            b.combat_until = self.now() + length
            caster = b.cls in ("priest", "mage")
            if caster and ("caster_stall" in self.bugs or "caster_casts_from_range" in self.bugs):
                b.fight_pos, b.action, b.trigger = None, "reach spell", "enemy out of spell range"
                if "caster_casts_from_range" in self.bugs:  # the stand goes on casting: a spell lands every 1.5 s
                    for k in range(1, int(length / 1.5)):
                        self.at(1.5 * k, lambda n=b.name: self._cast_landed(n, guid))
            else:
                d = math.hypot(c["x"] - b.x, c["y"] - b.y) or 1
                keep = 25 if caster else 1.5
                b.fight_pos = {"x": c["x"] + (b.x - c["x"]) * keep / d, "y": c["y"] + (b.y - c["y"]) * keep / d}
                b.action = "frostbolt" if caster else "melee"
        tank = next(n for n in self.party if self.bots[n].role == "tank")
        rogue = next(n for n in self.party if self.bots[n].cls == "rogue")
        for k in range(2, length - 2, 2):
            self.at(k, lambda: self._hit(tank, guid))
        if moon and "moon_never_fights" in self.bugs:  # never pulled in: no sheep either, and it is left alone
            pack = [g for g in pack if g != moon[1]]
            self.creatures[moon[1]]["in_combat"] = False
            moon = None
        if moon and moon[1] in self.creatures:
            mage, mg = moon
            mname = self.creatures[mg]["name"]
            join = lambda: self.emit("attack_start", source=mname, source_guid=mg, target=tank,
                                     target_guid="0x0000000000000001", party=True)
            joins = 8.0 if "moon_joins_late" in self.bugs else 0.5  # the pack-mate pulls it in at its own time
            if "moon_joins_late" in self.bugs:
                self.creatures[mg]["in_combat"] = False
                self.at(joins, lambda: self.creatures[mg].update(in_combat=True))
            self.at(joins, join)
            start = lambda: self.emit("spell_start", source=mage, spell_id=POLY_ID, target_guid=mg, party=True)
            go = lambda: self.emit("spell_go", source=mage, target=self.creatures[mg]["name"], target_guid=mg,
                                   spell_id=POLY_ID, party=True)
            aura = lambda: self.emit("aura_applied", target=self.creatures[mg]["name"], target_guid=mg,
                                     spell_id=POLY_ID, data={"harmful": True})
            if late:
                for k in (1, 6, 11):
                    self.at(k, start)
                self.at(3, lambda: self._hit(rogue, mg, 112))
                self.at(26, go)
                self.at(26.1, aura)
            elif "moon_joins_late" in self.bugs:  # 12 s after the pull, 4 s after the Moon mob's own combat
                self.at(9, start)
                self.at(12, go)
                self.at(12.1, aura)
            else:
                self.at(1, start)
                self.at(2.5, go)
                self.at(2.6, aura)
        self.at(length - 0.5, lambda: self._kill(pack))

    # -- dashboard
    def dashboard_bots(self):
        with self.lock:
            return [b.snapshot(self.now()) for b in self.bots.values()]


class FakeServers:
    """The world behind a turtle-agent JSON-RPC server and a dashboard server, on ephemeral ports."""

    def __init__(self, world: FakeWorld):
        self.world = world
        w = world

        class Rpc(BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    out = {"jsonrpc": "2.0", "id": req["id"], "result": w.handle(req["method"], req.get("params") or {})}
                except RpcError as e:
                    out = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": e.code, "message": e.msg}}
                body = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        class Dash(BaseHTTPRequestHandler):
            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._send({"ok": True})

            def do_GET(self):
                self._send(w.dashboard_bots() if self.path == "/api/v1/bots" else {}, 200 if self.path == "/api/v1/bots" else 404)

            def log_message(self, *a):
                pass

        self.rpc = ThreadingHTTPServer(("127.0.0.1", 0), Rpc)
        self.dash = ThreadingHTTPServer(("127.0.0.1", 0), Dash)
        for s in (self.rpc, self.dash):
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def rpc_addr(self):
        return f"127.0.0.1:{self.rpc.server_address[1]}"

    @property
    def dash_url(self):
        return f"http://127.0.0.1:{self.dash.server_address[1]}"

    def close(self):
        for s in (self.rpc, self.dash):
            s.shutdown()
            s.server_close()

"""walk_to: walk the real client to a point along the server's navmesh, with the keyboard.

Path: the server's own mmaps (navmesh.py: the real Detour when wowharness/native has it, else the offline
fallback), the same tiles the bots path on.

Pose: the 1.12 client has no world position in instances (GetPlayerMapPosition is 0,0 there; no
UnitPosition, no GetPlayerFacing on the 1.18.1 client), and the dashboard lists bots only. So position AND
orientation come from the server: `.gps` typed in chat (GM), its reply read off the live pixel channel,
about once per step. The server's orientation is the one the client last sent (turn start/stop, heartbeats
while running). When a fix has no orientation, the heading is taken from the motion between two fixes. If
the observer addon finds the minimap's player arrow (HX_WalkBegin reports facing=arrow), its facing
arrives at 10 Hz on the pixel strip and turns are closed-loop; else turns are timed holds at TURN_RATE.

Steering: turn with A/D (held for err / TURN_RATE), walk with W, re-fix each step, advance the waypoint
within `waypoint_radius` or once it is passed, stop before sharp corners and the goal.

Stuck, either of:
- no movement: the .gps position stayed within `still_dist` (0.5 yd) while W was held for `still_s` (2 s) in
  all, counted over every hop (the walker stops before corners and the goal, and to turn in place, so W is
  held in short pieces);
- no progress: the remaining distance has not shrunk by 1 yd in `stuck_s` of W held (in all, the same way;
  shuffling or sliding along a wall).
Then: an escalating ladder (jump forward; back off and side-step, alternating sides), and a re-plan from
where the player is.

Blocked: stuck again and again at the same spot (within 3 yd of the first stuck there) for `blocked_s`, once
the ladder has tried both side-steps there (`blocked_tries` stucks), however far the next waypoint is (a door
can sit in the middle of a long straight segment): a closed door or an object the navmesh does not have (it
holds no game objects). The walk gives up with "blocked (door or object?)", the spot in result["blocked"]
(at, ahead, waypoint, distance to it), and its "ahead" point can be passed back as `avoid=[...]` so the next
walk routes round it (python engine; Detour has no per-call polygon filter here).

Gives up after `max_unstuck` tries, when blocked, when no progress was made for `give_up_s`, or at
`timeout_s`, with the reason in the result.
"""
import math
import re
import time
from dataclasses import dataclass

from .navmesh import NoPath, dist2d, fmt, path_length

TURN_RATE = math.pi  # rad/s on the 1.18.1 client (tester: a 500 ms hold turns ~90 degrees)
RUN_SPEED = 7.0      # yd/s, player run speed
KEYS = {"forward": "w", "back": "s", "left": "a", "right": "d",
        "strafe_left": "q", "strafe_right": "e", "jump": "space"}
GPS_POS = re.compile(r"X: (-?\d+(?:\.\d+)?) Y: (-?\d+(?:\.\d+)?) Z: (-?\d+(?:\.\d+)?) Orientation: (-?\d+(?:\.\d+)?)")
GPS_MAP = re.compile(r"Map: (\d+) \(")


@dataclass
class Pose:
    x: float
    y: float
    z: float
    o: float | None  # orientation, radians, 0 = +x (north), counter-clockwise; None = unknown
    map_id: int | None
    t: float  # harness clock when the fix arrived
    src: str = "gps"

    @property
    def xyz(self):
        return (self.x, self.y, self.z)


# -- pure parts -------------------------------------------------------------------------------------------

def parse_gps(lines, t: float = 0.0) -> Pose | None:
    """The last `.gps` reply in some chat lines: 'Map: 70 (Uldaman) ...' then 'X: .. Y: .. Z: .. Orientation: ..'."""
    pose, map_id = None, None
    for line in lines:
        m = GPS_MAP.search(line)
        if m:
            map_id = int(m.group(1))
        m = GPS_POS.search(line)
        if m:
            x, y, z, o = (float(v) for v in m.groups())
            pose = Pose(x, y, z, o, map_id, t)
    return pose


def parse_pose_line(text: str) -> dict | None:
    """'HX|pose=<GetTime>,<facing>,<x>,<y>,<z>,<mapx>,<mapy>' (empty field = no source) -> dict."""
    i = text.find("HX|pose=")
    if i < 0:
        return None
    parts = text[i + 8:].split(",")
    if len(parts) < 7:
        return None
    def num(s):
        try:
            return float(s)
        except ValueError:
            return None
    vals = [num(p) for p in parts[:7]]
    return dict(zip(("gt", "facing", "x", "y", "z", "mx", "my"), vals))


def wrap(a: float) -> float:
    """Angle into [-pi, pi)."""
    return (a + math.pi) % (2 * math.pi) - math.pi


def heading(frm, to) -> float:
    """WoW orientation from one point to another (0 = +x, counter-clockwise towards +y)."""
    return math.atan2(to[1] - frm[1], to[0] - frm[0])


def turn_command(err: float, rate: float = TURN_RATE, deadband: float = 0.1, min_ms: int = 30):
    """Heading error (desired - current) -> (key, hold ms). Positive error turns left (counter-clockwise)."""
    if abs(err) < deadband:
        return None, 0
    return ("left" if err > 0 else "right"), max(min_ms, int(round(abs(err) / rate * 1000)))


def passed(a, b, p, lateral: float) -> bool:
    """p is beyond b along a->b, and within `lateral` of that line."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 < 1e-6:
        return True
    u = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2
    side = abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / math.sqrt(L2)
    return u >= 1.0 and side <= lateral


SHARP = 0.8  # rad: a corner sharper than this is walked up to and turned at standing still


def advance(path, i: int, pos, radius: float = 2.0, corner_radius: float = 0.8, clear=None) -> int:
    """Index of the waypoint to steer to: skip those within radius, or already passed. A sharp corner needs
    corner_radius and, when `clear(a, b)` is given, a free straight line on to the next waypoint, so the
    player does not cut the corner into the wall."""
    while i < len(path) - 1:
        d = dist2d(pos, path[i])
        if corner_angle(path, i) > SHARP:
            near = d <= 0.3 or (d <= corner_radius and (clear is None or clear(pos, path[i + 1])))
        else:
            near = d <= radius
        if near or (i > 0 and passed(path[i - 1], path[i], pos, 2 * radius)):
            i += 1
        else:
            break
    return i


def remaining(path, i: int, pos) -> float:
    return dist2d(pos, path[i]) + path_length(path[i:])


def corner_angle(path, i: int) -> float:
    """How sharply the path turns at waypoint i (0 = straight on)."""
    if i <= 0 or i >= len(path) - 1:
        return 0.0
    return abs(wrap(heading(path[i], path[i + 1]) - heading(path[i - 1], path[i])))


class Progress:
    """Stuck / hopeless detection on the remaining distance, best-so-far (a yard of shuffling is not progress).
    stuck(): no gain of `min_gain` within `window_s` of walking; the window runs on `w` (seconds W was held,
    in all) when given, so it carries across the walker's stop-and-go hops and turns in place, else on t.
    hopeless(): no new overall best for give_up_s (on t); a re-plan does not restart that clock."""

    def __init__(self, window_s: float = 3.0, min_gain: float = 1.0, give_up_s: float = 25.0):
        self.window_s, self.min_gain, self.give_up_s = window_s, min_gain, give_up_s
        self.best = self.window_best = math.inf
        self.best_t = self.window_t = 0.0

    def start(self, t: float, rem: float, w: float | None = None) -> None:
        self.best, self.best_t = rem, t
        self.restart(t if w is None else w, rem)

    def restart(self, w: float, rem: float) -> None:
        """A new walking window (after an unstick and re-plan)."""
        self.window_best, self.window_t = rem, w

    def replanned(self, rem: float) -> None:
        self.best = rem  # the new path may be longer; the clock keeps running

    def stuck(self, t: float, rem: float, w: float | None = None) -> bool:
        if rem < self.best - self.min_gain:
            self.best, self.best_t = rem, t
        w = t if w is None else w
        if rem < self.window_best - self.min_gain:
            self.window_best, self.window_t = rem, w
            return False
        return w - self.window_t >= self.window_s

    def hopeless(self, t: float) -> bool:
        return t - self.best_t >= self.give_up_s


# -- pose from the server ---------------------------------------------------------------------------------

class GpsPose:
    """Position and orientation from the server's `.gps` reply (GM command; it reports the SELECTED unit, so
    the target is put aside for the walk by HX_WalkBegin and restored by HX_WalkEnd). Optional 10 Hz facing
    from the addon's pose stream when it found the minimap arrow; positions from it when the client has
    UnitPosition."""
    name = "gps"

    def __init__(self, client, timeout_s: float = 3.0, clock=time.time):
        self.c, self.timeout_s, self.clock = client, timeout_s, clock
        self.walk_api = False
        self.facing_src = "none"
        self.pos_src = "none"
        self.arrow_offset = None
        self._pose_off = 0
        self._last = None  # (facing, x, y, z, clock)
        self.last_map = None
        self.notes: list[str] = []

    def begin(self) -> None:
        since = self.c.live_offset()
        self.c.run_lua("HX_WalkBegin()")
        line = self.c.wait_live(r"HX\|walk=begin", since, 2.5, "chat")
        if line:
            self.walk_api = True
            m = re.search(r"\|facing=(\w+)", line)
            self.facing_src = m.group(1) if m else "none"
            m = re.search(r"\|pos=(\w+)", line)
            self.pos_src = m.group(1) if m else "none"
            self._pose_off = since
        else:
            self.c.run_lua("ClearTarget()")
            self.notes.append("observer addon has no HX_WalkBegin (old copy?): target cleared, not restored; "
                              "no pose stream")

    def end(self) -> None:
        if self.walk_api:
            self.c.run_lua("HX_WalkEnd()")

    def _stream(self):
        """Newest pose-stream sample, if one arrived in the last 0.4 s."""
        lines, self._pose_off = self.c.live_lines(self._pose_off, "pose")
        for text in lines:
            p = parse_pose_line(text)
            if p:
                self._last = (p["facing"], p["x"], p["y"], p["z"], self.clock())
        if self._last and self.clock() - self._last[4] <= 0.4:
            return self._last
        return None

    def facing(self) -> float | None:
        """Live facing (arrow + calibrated offset), or None when there is no usable stream."""
        if self.facing_src == "none" or self.arrow_offset is None:
            return None
        s = self._stream()
        if not s or s[0] is None:
            return None
        return wrap(s[0] + self.arrow_offset)

    def read(self) -> Pose | None:
        if self.pos_src != "none":
            s = self._stream()
            if s and s[1] is not None:
                o = wrap(s[0] + (self.arrow_offset or 0.0)) if s[0] is not None else None
                return Pose(s[1], s[2], s[3], o, self.last_map, s[4], "pixel")
        since = self.c.live_offset()
        self.c.chat(".gps")
        if not self.c.wait_live(GPS_POS.pattern, since, self.timeout_s, "chat"):
            return None
        lines, _ = self.c.live_lines(since, "chat")
        pose = parse_gps(lines, self.clock())
        if pose is None:
            return None
        if pose.map_id is None:
            pose.map_id = self.last_map
        self.last_map = pose.map_id
        if self.facing_src != "none" and self.arrow_offset is None and pose.o is not None:
            s = self._stream()
            if s and s[0] is not None:  # calibrate the arrow against the server's orientation once
                self.arrow_offset = wrap(pose.o - s[0])
        return pose


# -- the walk ---------------------------------------------------------------------------------------------

class _Keys:
    def __init__(self, client, keys, sleep, clock=time.time):
        self.c, self.k, self.sleep, self.clock, self.down = client, keys, sleep, clock, set()
        self._since: dict[str, float] = {}
        self._total: dict[str, float] = {}

    def held_s(self, what: str) -> float:
        """Seconds the key has been held in all, so far (the current hold included)."""
        t = self._total.get(what, 0.0)
        if what in self.down:
            t += self.clock() - self._since[what]
        return t

    def _post(self, what: str, down: bool) -> None:
        name = self.k[what]
        f = getattr(self.c, "key_down" if down else "key_up", None)
        if f is not None:
            f(name)
        else:  # a driver still running an older Client (no key_down/key_up): the same window message
            from .keys import VK
            self.c._send("post_key", VK[name.lower()], down)

    def press(self, what: str) -> None:
        if what not in self.down:
            self._post(what, True)
            self.down.add(what)
            self._since[what] = self.clock()

    def release(self, what: str) -> None:
        if what in self.down:
            self._post(what, False)
            self.down.discard(what)
            self._total[what] = self._total.get(what, 0.0) + self.clock() - self._since.pop(what)

    def hold(self, what: str, ms: int) -> None:
        self.press(what)
        self.sleep(ms / 1000)
        self.release(what)

    def release_all(self) -> None:
        for what in list(self.down):
            self.release(what)


def _live_health(src) -> dict | None:
    """The live channel's health when a .gps read went unanswered (is it the channel or the server?)."""
    fn = getattr(getattr(src, "c", None), "live_health", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception as err:
        return {"ok": None, "reason": f"{type(err).__name__}: {err}"}


def walk_to(client, pose_source, x: float, y: float, z: float, map_id: int | None = None, *, tiles=None, mesh=None,
            arrive: float = 2.0, waypoint_radius: float = 2.0, timeout_s: float | None = None, tick_s: float = 0.5,
            stuck_s: float = 3.0, still_s: float = 2.0, still_dist: float = 0.5, max_unstuck: int = 5,
            give_up_s: float = 25.0, blocked_s: float = 8.0, blocked_tries: int = 3, avoid=None,
            avoid_r: float = 3.0, turn_rate: float = TURN_RATE, margin: float = 0.6, engine: str = "auto",
            keys: dict | None = None, clock=time.time, sleep=time.sleep, max_trace: int = 400) -> dict:
    """Walk the player to (x, y, z) on map_id along the navmesh. pose_source: an object with read() -> Pose
    (and optional begin(), end(), facing()); None = GpsPose(client). tiles: a navmesh.TileCache (or give
    `mesh`). avoid: points [(x, y, z)] whose navmesh polygons within avoid_r are not routed through (a door
    a previous walk reported as blocked); it makes an "auto" engine the python one. Returns ok/reason, the
    path, a trace of fixes (t, x, y, z, o, waypoint), stuck events (why: "no movement" / "no progress"),
    "blocked" when it gave up at a door or object, and timings. Keys are always released, and the target
    restored, however it ends."""
    src = pose_source or GpsPose(client, clock=clock)
    k = _Keys(client, {**KEYS, **(keys or {})}, sleep, clock)
    avoid = [tuple(float(c) for c in p) for p in (avoid or [])]
    if avoid and engine == "auto":
        engine = "python"  # the Detour wrapper has no per-call polygon filter
    goal = (x, y, z)
    t0 = clock()
    out = {"ok": False, "reason": "", "goal": goal, "map": map_id, "path": [], "trace": [], "stuck": [],
           "replans": 0, "turns": 0, "pose_source": getattr(src, "name", type(src).__name__), "engine": None}
    speed = RUN_SPEED
    facing_corrections = 0

    def note(t, pose, i, moving):
        if len(out["trace"]) < max_trace:
            out["trace"].append({"t": round(t - t0, 2), "x": round(pose.x, 1), "y": round(pose.y, 1),
                                 "z": round(pose.z, 1), "o": None if pose.o is None else round(pose.o, 2),
                                 "wp": i, "moving": moving, "src": pose.src})

    def plan(frm):
        p = mesh.find_path(frm, goal, margin=margin, **({"avoid": avoid, "avoid_r": avoid_r} if avoid else {}))
        out["path"] = [tuple(round(c, 1) for c in q) for q in p]
        return p

    if hasattr(src, "begin"):
        src.begin()
    try:
        fix = src.read()
        if fix is None:
            out["reason"] = "no position: no .gps reply within the timeout (GM rights? live pixel channel running?)"
            out["live_health"] = _live_health(src)
            return out
        out["start"] = tuple(round(c, 1) for c in fix.xyz)
        if map_id is None:
            map_id = out["map"] = fix.map_id
        if fix.map_id is not None and map_id is not None and fix.map_id != map_id:
            out["reason"] = f"the player is on map {fix.map_id}, the goal is on map {map_id}"
            return out
        if mesh is None:
            if tiles is None:
                out["reason"] = "no navmesh source (tiles)"
                return out
            try:
                mesh = tiles.mesh_for(map_id, [fix.xyz, goal], engine=engine)
            except (NoPath, OSError, ValueError) as err:
                out["reason"] = f"navmesh: {err}"
                return out
        out["engine"] = getattr(mesh, "engine", "?")
        try:
            path = plan(fix.xyz)
        except NoPath as err:
            out["reason"] = f"no path: {err}"
            return out
        total = path_length(path)
        if timeout_s is None:
            timeout_s = 30 + 3 * total / RUN_SPEED
        out["timeout_s"] = round(timeout_s, 1)
        o = fix.o
        i, moving, unstucks, misses = 1, False, 0, 0
        read_s = 0.4  # how long a fix takes (typing .gps, the reply, the pixel channel); measured below
        prog = Progress(stuck_s, 1.0, give_up_s)
        prog.start(clock(), remaining(path, min(i, len(path) - 1), fix.xyz), 0.0)
        prev_fix, straight_since_prev = None, False
        first = True
        # no-movement detector: where the player was, and how long W had been held in all, at that point
        still_at, still_w = fix.xyz, 0.0
        blocked = None  # [first stuck time, spot, stucks there] of repeated stuck events at one spot
        while True:
            now = clock()
            if now - t0 > timeout_s:
                out["reason"] = f"timeout after {timeout_s:.0f} s"
                break
            if not first:
                r0 = clock()
                fix = src.read()
                read_s = 0.7 * read_s + 0.3 * min(2.0, clock() - r0)
                if fix is None:
                    misses += 1
                    if misses >= 3:
                        out["reason"] = "lost position: 3 .gps reads without a reply"
                        out["live_health"] = _live_health(src)
                        break
                    continue
                misses = 0
            first = False
            if fix.map_id is not None and map_id is not None and fix.map_id != map_id:
                out["reason"] = f"map changed to {fix.map_id} (loading screen, death or teleport?)"
                break
            # facing: the server's orientation; the motion between two fixes when it has none
            if prev_fix is not None and straight_since_prev and dist2d(prev_fix.xyz, fix.xyz) >= 2.0:
                m = heading(prev_fix.xyz, fix.xyz)
                if fix.o is None:
                    fix.o = m
                elif abs(wrap(m - fix.o)) > 0.35:
                    facing_corrections += 1  # sliding along a wall, or a stale orientation
                dt = fix.t - prev_fix.t
                if 0.2 < dt < 3:
                    speed = min(14.0, max(2.0, 0.7 * speed + 0.3 * dist2d(prev_fix.xyz, fix.xyz) / dt))
            o = fix.o if fix.o is not None else o
            live = src.facing() if hasattr(src, "facing") else None
            if live is not None:
                o = live
            prev_fix, straight_since_prev = fix, moving
            pos = fix.xyz
            if moving:  # the player kept walking since the fix arrived
                lag = clock() - fix.t + 0.15
                pos = (pos[0] + speed * lag * math.cos(o or 0), pos[1] + speed * lag * math.sin(o or 0), pos[2])
            note(clock(), fix, i, moving)
            end = path[-1]
            if dist2d(fix.xyz, end) <= arrive and abs(fix.z - end[2]) < 5:
                out["ok"], out["reason"] = True, "arrived"
                break
            if o is None:
                out["reason"] = "no orientation in the .gps reply and no motion to take it from"
                break
            i = advance(path, i, pos, waypoint_radius, clear=getattr(mesh, "clear", None))
            rem = remaining(path, i, pos)
            now = clock()
            # raw fixes only: the dead-reckoned `pos` moves on with the facing even while the player stands
            w_held = k.held_s("forward")
            if dist2d(fix.xyz, still_at) >= still_dist:
                still_at, still_w = fix.xyz, w_held
            why = None
            if w_held - still_w >= still_s:
                why = "no movement"
            elif prog.stuck(now, remaining(path, i, fix.xyz), w_held):
                why = "no progress"
            if why:
                k.release("forward")
                moving = False
                unstucks += 1
                d_wp = dist2d(fix.xyz, path[i])
                out["stuck"].append({"t": round(now - t0, 1), "at": fmt(fix.xyz), "waypoint": i,
                                     "remaining": round(rem, 1), "to_waypoint": round(d_wp, 1), "why": why,
                                     "try": unstucks})
                if blocked is None or dist2d(fix.xyz, blocked[1]) > 3.0:
                    blocked = [now, fix.xyz, 1]
                elif blocked[2] >= blocked_tries and now - blocked[0] >= blocked_s:
                    wp = path[i]
                    u = min(1.0, 1.5 / max(d_wp, 1e-6))
                    ahead = (fix.x + u * (wp[0] - fix.x), fix.y + u * (wp[1] - fix.y), fix.z)
                    out["blocked"] = {"at": tuple(round(c, 1) for c in fix.xyz),
                                      "ahead": tuple(round(c, 1) for c in ahead),
                                      "waypoint": tuple(round(c, 1) for c in wp), "to_waypoint": round(d_wp, 1),
                                      "stucks": blocked[2] + 1, "seconds": round(now - blocked[0], 1)}
                    out["reason"] = (f"blocked (door or object?) at {fmt(fix.xyz)}: {blocked[2] + 1} stucks there in "
                                     f"{now - blocked[0]:.0f} s, {d_wp:.1f} yd before waypoint {fmt(wp)}; the navmesh "
                                     f"has no game objects; to route round it walk again with avoid=[{fmt(ahead)}]")
                    break
                else:
                    blocked[2] += 1
                if unstucks > max_unstuck:
                    out["reason"] = f"stuck: {max_unstuck} unstick tries did not get past {fmt(fix.xyz)}"
                    break
                _unstick(k, unstucks, sleep)
                still_w = k.held_s("forward")  # the ladder's own W does not count
                fix = src.read()
                if fix is None:
                    continue
                try:
                    path = plan(fix.xyz)
                except NoPath as err:
                    out["reason"] = f"no path after unstick: {err}"
                    break
                out["replans"] += 1
                i = 1
                still_at = fix.xyz
                prog.replanned(remaining(path, 1, fix.xyz))
                prog.restart(still_w, remaining(path, 1, fix.xyz))
                first, prev_fix = True, None
                continue
            if prog.hopeless(now):
                out["reason"] = f"no progress for {give_up_s:.0f} s (at {fmt(fix.xyz)}, {rem:.1f} yd to go)"
                break
            target = path[i]
            d_wp = dist2d(pos, target)
            err = wrap(heading(pos, target) - o)
            if abs(err) > 1.2 or (d_wp < 4 and abs(err) > 0.6):
                if moving:  # sharp turn: stop, turn in place
                    k.release("forward")
                    moving = False
                o = _turn(k, src, o, err, turn_rate, sleep, clock)
                out["turns"] += 1
                straight_since_prev = False
            elif abs(err) > 0.1:
                o = _turn(k, src, o, err, turn_rate, sleep, clock)
                out["turns"] += 1
                straight_since_prev = False
            # walk on for a step (the next fix is read while walking), or, when the goal or a sharp corner
            # comes within that, just up to it and stop
            stop_here = (i == len(path) - 1 or corner_angle(path, i) > SHARP) and d_wp / speed <= tick_s + read_s
            run_s = max(0.1, d_wp / speed - 0.05) if stop_here else tick_s
            if not moving:
                k.press("forward")
                moving = True
            sleep(run_s)
            if stop_here:
                k.release("forward")
                moving = False
                straight_since_prev = False
        out["final"] = tuple(round(c, 1) for c in fix.xyz) if fix else None
        if fix:
            out["dist_left"] = round(dist2d(fix.xyz, path[-1]) if out["path"] else dist2d(fix.xyz, goal), 1)
        return out
    finally:
        k.release_all()
        if hasattr(src, "end"):
            try:
                src.end()
            except Exception as err:  # never hide the walk's own result
                out.setdefault("notes", []).append(f"end: {type(err).__name__}: {err}")
        out["seconds"] = round(clock() - t0, 1)
        out["speed_est"] = round(speed, 2)
        out["facing_corrections"] = facing_corrections
        notes = list(getattr(src, "notes", []))
        if notes:
            out.setdefault("notes", []).extend(notes)


def _turn(k: _Keys, src, o: float, err: float, rate: float, sleep, clock) -> float:
    """Turn by err; returns the new facing estimate. Closed-loop on the live facing when there is one."""
    key, ms = turn_command(err, rate)
    if not key:
        return o
    live = src.facing() if hasattr(src, "facing") else None
    if live is None:
        k.hold(key, ms)
        return wrap(o + math.copysign(ms / 1000 * rate, err))
    target = wrap(live + err)
    k.press(key)
    t_end = clock() + ms / 1000 * 1.5 + 0.2
    f = live
    while clock() < t_end:
        sleep(0.02)
        f = src.facing()
        if f is None or abs(wrap(target - f)) < 0.05 or wrap(target - f) * err < 0:
            break
    k.release(key)
    if f is None or abs(wrap(f - live)) < 0.5 * abs(err):
        # the arrow did not follow the turn (rotating minimap?): stop trusting it
        if hasattr(src, "facing_src"):
            src.facing_src = "none"
            src.notes.append("minimap-arrow facing did not follow a turn; using .gps orientation only")
        return wrap(o + err)
    return f


def _unstick(k: _Keys, attempt: int, sleep) -> None:
    """Escalating ladder: jump forward; then back off and side-step, alternating sides, longer each time."""
    if attempt == 1:
        k.press("forward")
        k.hold("jump", 80)
        sleep(0.6)
        k.release("forward")
        return
    long = attempt >= 4
    k.hold("back", 1000 if long else 600)
    k.hold("strafe_left" if attempt % 2 == 0 else "strafe_right", 1200 if long else 700)
    if long:
        k.hold("jump", 80)


def plan_path(tiles, map_id: int, start, goal, margin: float = 0.6, engine: str = "auto", avoid=None,
              avoid_r: float = 3.0) -> dict:
    """The path walk_to would take, without walking: for checking a route first."""
    if avoid and engine == "auto":
        engine = "python"
    mesh = tiles.mesh_for(map_id, [start, goal], engine=engine)
    try:
        p = mesh.find_path(start, goal, margin=margin, **({"avoid": avoid, "avoid_r": avoid_r} if avoid else {}))
    except NoPath as err:
        return {"ok": False, "reason": str(err), "engine": mesh.engine}
    return {"ok": True, "engine": mesh.engine, "length": round(path_length(p), 1),
            "straight": round(math.dist(start, goal), 1), "path": [tuple(round(c, 1) for c in q) for q in p]}


def tiles_for(cfg, cache_dir: str | None = None):
    """A TileCache for the server in a harness Config (ssh, mmaps folder, container), cached under
    ~/.wowharness-mmaps. Works with older Config objects that lack the walk_to settings."""
    import os
    from .navmesh import DEFAULT_REMOTE_DIR, SshTiles, TileCache
    if not cfg.server_ssh:
        raise ValueError("walk_to needs server_ssh in the settings (tiles are read over ssh)")
    cache = cache_dir or getattr(cfg, "mmaps_cache", "") or os.path.join(os.path.expanduser("~"), ".wowharness-mmaps")
    src = SshTiles(cfg.server_ssh, getattr(cfg, "server_mmaps", "") or DEFAULT_REMOTE_DIR, cfg.server_container)
    return TileCache(cache, src, cfg.server_host)


def walk(client, x: float, y: float, z: float, map_id: int | None = None, **kw) -> dict:
    """walk_to with the tiles of the client's own server (from client.cfg): the one call for a driver."""
    return walk_to(client, None, x, y, z, map_id, tiles=tiles_for(client.cfg), **kw)


def brief(result: dict, last: int = 5) -> dict:
    """A short view of a walk_to result (driver output is cut at 6000 characters)."""
    keep = ("ok", "reason", "seconds", "start", "final", "dist_left", "map", "engine", "replans", "turns",
            "speed_est", "facing_corrections", "blocked", "notes")
    out = {k: result[k] for k in keep if k in result}
    out["path_points"] = len(result.get("path") or [])
    out["stuck"] = result.get("stuck", [])[-3:]
    out["trace_last"] = (result.get("trace") or [])[-last:]
    return out

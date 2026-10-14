import math
import re

import pytest

from wowharness import navmesh as nm
from wowharness import navwalk as nw
from navfixture import build_tile, l_corridor, ring

GPS_LINES = [
    "10/12 12:00:00.100  You are INdoor",
    "10/12 12:00:00.100  Map: 70 (Uldaman) Zone: 1337 (Uldaman) Area: 1337 (Uldaman)",
    "10/12 12:00:00.100  X: -226.799995 Y: 49.099998 Z: -46.000000 Orientation: 1.570000",
    "10/12 12:00:00.100  grid[33,32]cell[3,2] InstanceID: 12",
    "10/12 12:00:00.100   ZoneX: 0.000000 ZoneY: 0.000000",
    "10/12 12:00:00.100  GroundZ: -46.000000 FloorZ: -46.000000 Have height data (Map: 1 VMap: 1)",
    "10/12 12:00:00.100  TileX: 31, TileY: 32",
]


def test_parse_gps_reads_position_orientation_and_map():
    p = nw.parse_gps(GPS_LINES, t=5.0)
    assert (round(p.x, 1), round(p.y, 1), p.z, p.o, p.map_id, p.t) == (-226.8, 49.1, -46.0, 1.57, 70, 5.0)
    assert nw.parse_gps(["nothing here"]) is None


def test_parse_pose_line_with_and_without_sources():
    p = nw.parse_pose_line("HX|pose=1234.50,1.571,,,,0.0000,0.0000")
    assert p["facing"] == 1.571 and p["x"] is None and p["mx"] == 0.0
    p = nw.parse_pose_line("HX|pose=1.00,,10.5,-3.2,7.0,,")
    assert p["facing"] is None and (p["x"], p["y"], p["z"]) == (10.5, -3.2, 7.0)
    assert nw.parse_pose_line("HX|p=Me,100") is None


def test_heading_and_wrap_follow_wow_orientation():
    assert nw.heading((0, 0), (1, 0)) == 0.0  # +x = north = 0
    assert math.isclose(nw.heading((0, 0), (0, 1)), math.pi / 2)  # +y = west = counter-clockwise
    assert math.isclose(nw.wrap(3 * math.pi / 2), -math.pi / 2)
    assert math.isclose(nw.wrap(-3 * math.pi / 2), math.pi / 2)


def test_turn_command_key_and_hold_from_the_turn_rate():
    assert nw.turn_command(math.pi / 2) == ("left", 500)  # 90 degrees at pi rad/s
    assert nw.turn_command(-math.pi / 4) == ("right", 250)
    assert nw.turn_command(0.05) == (None, 0)
    assert nw.turn_command(0.11) == ("left", 35)
    assert nw.turn_command(-1.0, rate=2.0) == ("right", 500)


def test_advance_skips_reached_and_passed_waypoints():
    gentle = [(0, 0, 0), (10, 0, 0), (20, 3, 0), (30, 3, 0)]
    assert nw.advance(gentle, 1, (5, 0)) == 1
    assert nw.advance(gentle, 1, (8.5, 0.5)) == 2  # within 2 yd of a gentle bend
    path = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (20, 10, 0)]
    assert nw.advance(path, 1, (8.5, 0.5)) == 1  # a sharp corner needs 0.8 yd: no cutting it into the wall
    assert nw.advance(path, 1, (9.5, 0.3)) == 2
    assert nw.advance(path, 1, (11, 1)) == 2  # passed it (beyond, close to the line)
    assert nw.advance(path, 1, (11, 8)) == 1  # beyond but far off the line: not passed
    assert nw.advance(path, 3, (20, 10)) == 3  # the goal is never skipped


def test_corner_angle():
    path = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (20, 20, 0)]
    assert math.isclose(nw.corner_angle(path, 1), math.pi / 2)
    assert math.isclose(nw.corner_angle(path, 2), math.pi / 4)
    assert nw.corner_angle(path, 0) == 0.0


def test_progress_stuck_and_hopeless():
    p = nw.Progress(window_s=3, min_gain=1, give_up_s=10)
    p.start(0, 50)
    assert not p.stuck(1, 45)
    assert not p.stuck(3.5, 44.5)  # shuffling, but the window started at the last real gain (t=1)
    assert p.stuck(4.1, 44.6)
    p.restart(5, 44.6)
    assert not p.stuck(6, 44.6)
    assert not p.hopeless(10.9) and p.hopeless(11.1)  # best was set at t=1
    p.replanned(60)
    assert p.hopeless(11.1)  # a re-plan does not restart the clock


def test_progress_window_runs_on_w_held_time_across_hops():
    p = nw.Progress(window_s=3, min_gain=1, give_up_s=25)
    p.start(0, 20, 0.0)
    # stop-and-go: 0.5 s of W per 1 s step, no gain; the window counts only W time and is not restarted
    assert not p.stuck(5, 19.8, 2.5)
    assert p.stuck(7, 19.9, 3.5)
    p.restart(3.5, 19.9)
    assert not p.stuck(8, 18.5, 4.0)  # a real gain starts a new window


def test_keys_count_how_long_a_key_was_held():
    t = [0.0]
    k = nw._Keys(type("C", (), {"key_down": lambda s, n: None, "key_up": lambda s, n: None})(), nw.KEYS,
                 lambda s: t.__setitem__(0, t[0] + s), lambda: t[0])
    k.hold("forward", 500)
    t[0] += 3  # released: does not count
    k.press("forward")
    t[0] += 0.25
    assert math.isclose(k.held_s("forward"), 0.75) and k.held_s("back") == 0.0


# -- closed loop: a simulated client on the L corridor ----------------------------------------------------

class SimClient:
    """A player that moves like the 1.12 client (run 7 yd/s, turn pi rad/s, back 4.5, strafe 7) on a walkable
    area; answers .gps the way the server does. Walls (`blocks`) can be added that the navmesh does not know."""

    def __init__(self, x, y, o, polys, blocks=(), map_id=1):
        self.x, self.y, self.z, self.o, self.t = x, y, 0.0, o, 1000.0
        self.polys, self.blocks, self.map_id = polys, list(blocks), map_id
        self.held, self.lines, self.sent, self.blocked, self.slid = set(), [], [], 0, 0

    # client surface used by walk_to
    def clock(self):
        return self.t

    def sleep(self, dt):
        steps = max(1, int(dt / 0.02))
        for _ in range(steps):
            self._step(dt / steps)

    def key_down(self, name):
        self.held.add(name)

    def key_up(self, name):
        self.held.discard(name)

    def chat(self, text):
        self.sent.append(text)
        self.sleep(0.3)  # typing, the server round trip and the pixel channel
        if text == ".gps":
            self.lines += ["Map: %d (Test) Zone: 1 (T) Area: 1 (T)" % self.map_id,
                           "X: %f Y: %f Z: %f Orientation: %f" % (self.x, self.y, self.z, self.o % (2 * math.pi))]

    def run_lua(self, code):
        self.sent.append("/script " + code)
        if code.startswith("HX_WalkBegin"):
            self.lines.append("HX|walk=begin|target=Kel|facing=none|pos=none")

    def live_offset(self):
        return len(self.lines)

    def live_lines(self, since, kind=None):
        return (self.lines[since:] if kind in (None, "chat") else []), len(self.lines)

    def wait_live(self, pattern, since, timeout_s, kind=None):
        return next((l for l in self.lines[since:] if re.search(pattern, l)), None)

    # physics
    def _free(self, x, y):
        return any(nm.point_in_poly(x, y, v) for v in self.polys) and not any(
            x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in self.blocks)

    def _step(self, dt):
        self.t += dt
        if "a" in self.held:
            self.o += math.pi * dt
        if "d" in self.held:
            self.o -= math.pi * dt
        vx = vy = 0.0
        for key, speed, ang in (("w", 7, 0), ("s", 4.5, math.pi), ("q", 7, math.pi / 2), ("e", 7, -math.pi / 2)):
            if key in self.held:
                vx += speed * math.cos(self.o + ang)
                vy += speed * math.sin(self.o + ang)
        nx, ny = self.x + vx * dt, self.y + vy * dt
        if (vx or vy) and not self._free(nx, ny):
            # slide along the wall like the client: try each axis alone
            self.slid += 1
            if self._free(nx, self.y):
                ny = self.y
            elif self._free(self.x, ny):
                nx = self.x
            else:
                self.blocked += 1
                return
        self.x, self.y = nx, ny


def corridor_mesh(engine):
    m = nm.NavMesh(1) if engine == "python" else nm.DetourMesh(1, __import__("navfixture").mmap_params())
    m.add_tile("t.mmtile", build_tile(l_corridor()))
    return m


ENGINES = ["python"] + (["detour"] if nm.detour_lib() is not None else [])


@pytest.mark.parametrize("engine", ENGINES)
def test_walk_to_follows_the_corridor_round_the_corner(engine):
    sim = SimClient(12, 12, math.pi, l_corridor())  # facing the wrong way at the start
    r = nw.walk_to(sim, None, 48, 48, 0, 1, mesh=corridor_mesh(engine), clock=sim.clock, sleep=sim.sleep)
    assert r["ok"], r["reason"]
    assert math.dist((sim.x, sim.y), (48, 48)) <= 2.0
    assert r["engine"] == engine and r["stuck"] == [] and len(r["path"]) == 3
    assert sim.slid == 0 and sim.blocked == 0  # never touched a wall, not even at the corner
    assert sim.held == set()  # every key released
    assert sim.sent[0] == "/script HX_WalkBegin()" and sim.sent[-1] == "/script HX_WalkEnd()"
    assert r["seconds"] < 30 and r["trace"][0]["x"] == 12.0


def test_walk_to_unsticks_then_gives_up_at_a_wall_the_navmesh_does_not_know():
    wall = (45.5, 30, 50.5, 31)  # across the whole column, not in the mesh
    sim = SimClient(12, 12, 0.0, l_corridor(), blocks=[wall])
    r = nw.walk_to(sim, None, 48, 48, 0, 1, mesh=corridor_mesh("python"), clock=sim.clock, sleep=sim.sleep,
                   max_unstuck=2, give_up_s=60)
    assert not r["ok"] and r["reason"].startswith("stuck")
    assert len(r["stuck"]) == 3 and r["replans"] == 2
    assert all(abs(s["remaining"] - 19) < 3 for s in r["stuck"])  # stuck at the wall, 17-19 yd short
    assert sim.held == set() and sim.y < 31


def test_walk_to_unsticks_when_it_stands_still_in_the_stop_and_go_hops():
    # a crate the navmesh does not know, 5.8 yd before the sharp corner: the walker walks there in short hops
    # (it stops before sharp corners), so W is never held for long; before the fix no stuck check fired and
    # the walk ended "no progress for 25 s" with no unstick try (the Uldaman stall at (-244, 213))
    crate = (41, 12.2, 42, 14)
    sim = SimClient(12, 12, 0.0, l_corridor(), blocks=[crate])
    r = nw.walk_to(sim, None, 48, 48, 0, 1, mesh=corridor_mesh("python"), clock=sim.clock, sleep=sim.sleep)
    assert r["ok"], r["reason"]
    assert r["stuck"] and r["stuck"][0]["why"] == "no movement" and r["stuck"][0]["to_waypoint"] < 6.3
    assert r["replans"] == len(r["stuck"]) and "blocked" not in r
    assert sim.held == set()


def test_no_movement_fires_after_two_seconds_of_w():
    # walking straight into the wall the navmesh does not know: first unstick after ~2 s of W at the wall
    sim = SimClient(12, 12, 0.0, l_corridor(), blocks=[(30, 10, 31, 14)])
    r = nw.walk_to(sim, None, 48, 48, 0, 1, mesh=corridor_mesh("python"), clock=sim.clock, sleep=sim.sleep,
                   max_unstuck=1)
    first = r["stuck"][0]
    assert first["why"] == "no movement"
    at_wall = next(f["t"] for f in r["trace"] if f["x"] >= 29.5)
    assert 2.0 <= first["t"] - at_wall <= 3.5


def test_walk_to_gives_up_blocked_at_a_closed_door_and_reports_it():
    door = (45.5, 44, 50.5, 45)  # across the column 4 yd before the goal (the Seal of Khaz'Mul case)
    sim = SimClient(12, 12, 0.0, l_corridor(), blocks=[door])
    r = nw.walk_to(sim, None, 48, 48, 0, 1, mesh=corridor_mesh("python"), clock=sim.clock, sleep=sim.sleep)
    assert not r["ok"] and r["reason"].startswith("blocked (door or object?) at (")
    b = r["blocked"]
    assert 43 <= b["at"][1] < 44 and 44 <= b["ahead"][1] <= 46 and b["waypoint"] == (48.0, 48.0, 0.0)
    assert b["seconds"] >= 8 and "avoid=[" in r["reason"]
    assert all(s["to_waypoint"] <= 5 for s in r["stuck"]) and len(r["stuck"]) < 5
    assert sim.held == set() and "blocked" in nw.brief(r)


def test_walk_to_gives_up_blocked_at_a_door_in_the_middle_of_a_long_segment():
    # the Seal of Khaz'Mul case on the VM: the door 18 yd before the next waypoint, so "near the waypoint"
    # never held and the walk ran into its timeout with 5 stucks at the same spot
    door = (45.5, 30, 50.5, 31)
    sim = SimClient(12, 12, 0.0, l_corridor(), blocks=[door])
    r = nw.walk_to(sim, None, 48, 48, 0, 1, mesh=corridor_mesh("python"), clock=sim.clock, sleep=sim.sleep)
    assert not r["ok"] and r["reason"].startswith("blocked (door or object?) at ("), r["reason"]
    b = r["blocked"]
    assert 29 <= b["at"][1] < 30 and 30 <= b["ahead"][1] <= 31.5 and b["waypoint"] == (48.0, 48.0, 0.0)
    assert 17 <= b["to_waypoint"] <= 19 and b["stucks"] == 4 and b["seconds"] >= 8
    assert len(r["stuck"]) == 4 and all(s["to_waypoint"] > 17 for s in r["stuck"])
    assert r["seconds"] < r["timeout_s"] and sim.held == set()


def ring_mesh():
    m = nm.NavMesh(1)
    m.add_tile("t.mmtile", build_tile(ring()))
    return m


def test_find_path_keeps_clear_of_avoid_points():
    m = ring_mesh()
    short = m.find_path((12, 12, 0), (30, 48, 0))
    assert max(p[0] for p in short[:-1]) < 20  # up the left column
    long = m.find_path((12, 12, 0), (30, 48, 0), avoid=[(12, 44, 0)])
    assert min(p[0] for p in long[1:-1]) > 40  # round by the right
    with pytest.raises(nm.NoPath, match="avoid"):
        m.find_path((12, 12, 0), (30, 48, 0), avoid=[(12, 44, 0), (48, 30, 0)])
    if nm.detour_lib() is not None:
        with pytest.raises(nm.NoPath, match="python"):
            corridor_mesh("detour").find_path((12, 12, 0), (48, 48, 0), avoid=[(48, 30, 0)])


def test_walk_to_routes_round_a_blocked_door_on_the_next_walk():
    door = (10, 42, 14, 43)  # in the left column, 4.8 yd before its corner
    sim = SimClient(12, 12, 0.0, ring(), blocks=[door])
    r = nw.walk_to(sim, None, 30, 48, 0, 1, mesh=ring_mesh(), clock=sim.clock, sleep=sim.sleep)
    assert not r["ok"] and r["reason"].startswith("blocked")
    r2 = nw.walk_to(sim, None, 30, 48, 0, 1, mesh=ring_mesh(), clock=sim.clock, sleep=sim.sleep,
                    avoid=[r["blocked"]["ahead"]])
    assert r2["ok"], r2["reason"]
    assert r2["stuck"] == [] and max(p[0] for p in r2["path"]) > 40 and sim.held == set()


def test_walk_to_stops_on_the_wrong_map():
    sim = SimClient(12, 12, 0.0, l_corridor(), map_id=2)
    r = nw.walk_to(sim, None, 48, 48, 0, 1, mesh=corridor_mesh("python"), clock=sim.clock, sleep=sim.sleep)
    assert not r["ok"] and "map 2" in r["reason"] and sim.held == set()


def test_walk_to_reports_no_path_for_an_off_mesh_goal():
    sim = SimClient(12, 12, 0.0, l_corridor())
    r = nw.walk_to(sim, None, 200, 200, 0, 1, mesh=corridor_mesh("python"), clock=sim.clock, sleep=sim.sleep)
    assert not r["ok"] and r["reason"].startswith("no path") and "/script HX_WalkEnd()" in sim.sent


def test_walk_to_without_the_addon_helper_clears_the_target_and_says_so():
    sim = SimClient(12, 12, 0.0, l_corridor())
    sim.run_lua = lambda code: sim.sent.append("/script " + code)  # an old addon: no HX|walk=begin line
    r = nw.walk_to(sim, None, 48, 48, 0, 1, mesh=corridor_mesh("python"), clock=sim.clock, sleep=sim.sleep)
    assert r["ok"] and "/script ClearTarget()" in sim.sent and "/script HX_WalkEnd()" not in sim.sent
    assert any("HX_WalkBegin" in n for n in r["notes"])


class FollowDash:
    """Bots that stand still at the start: their distance grows as the player walks away."""

    def bots(self, names):
        return [{"name": n, "x": 12.0, "y": 12.0, "state": "follow", "last_action": "follow"} for n in names]


def test_scenario_walk_reports_bot_distances(monkeypatch):
    from wowharness import scenarios
    sim = SimClient(12, 12, 0.0, l_corridor())
    monkeypatch.setattr(scenarios.time, "sleep", lambda s: None)
    r = scenarios.walk(sim, FollowDash(), ["Kel"], 48, 48, 0, 1, mesh=corridor_mesh("python"),
                       clock=sim.clock, sleep=sim.sleep, interval_s=0.3)
    assert r["walk"]["ok"] and r["follow"] and r["follow"][0]["bot"] == "Kel"
    assert "Kel" in r["max_dist"] and "Kel" in r["decisions"]


def test_keys_fall_back_to_window_messages_on_an_older_client():
    class OldClient:
        def __init__(self):
            self.sent = []

        def _send(self, method, *args):
            self.sent.append((method, *args))

    c = OldClient()
    k = nw._Keys(c, nw.KEYS, lambda s: None)
    k.hold("left", 100)
    assert c.sent == [("post_key", 0x41, True), ("post_key", 0x41, False)]


def test_tiles_for_an_older_config_and_brief():
    from wowharness.config import Config
    cfg = Config("c", "192.0.2.11", 7878, "u", "p", "a", "pw", (0, 0), (0, 0), "tortoise-mangosd",
                 server_ssh=("pk@host",))
    t = nw.tiles_for(cfg, cache_dir="/tmp/x")
    assert t.source.dir == "~/tortoise-wow-server/data/mmaps" and t.source.container == "tortoise-mangosd"
    assert t.dir.endswith("192.0.2.11")
    b = nw.brief({"ok": True, "reason": "arrived", "path": [1, 2, 3], "trace": list(range(9)), "stuck": []})
    assert b["path_points"] == 3 and b["trace_last"] == [4, 5, 6, 7, 8]


def test_live_health_is_attached_when_gps_reads_go_unanswered():
    from types import SimpleNamespace
    from wowharness.navwalk import _live_health
    ok = SimpleNamespace(c=SimpleNamespace(live_health=lambda: {"ok": False, "reason": "no new page for 9.0 s"}))
    assert _live_health(ok) == {"ok": False, "reason": "no new page for 9.0 s"}
    assert _live_health(SimpleNamespace()) is None
    def boom():
        raise RuntimeError("x")
    assert _live_health(SimpleNamespace(c=SimpleNamespace(live_health=boom))) == {"ok": None, "reason": "RuntimeError: x"}

import math
import os

import pytest

from wowharness import navmesh as nm
from navfixture import build_tile, l_corridor, mmap_params

START, GOAL, CORNER = (12, 12, 0), (48, 48, 0), (46, 14)


def on_mesh(polys, p):
    return any(nm.point_in_poly(p[0], p[1], v) for v in polys)


def segment_on_mesh(polys, a, b, step=0.25):
    n = max(1, int(math.dist(a, b) / step))
    return all(on_mesh(polys, (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n)) for k in range(n + 1))


def meshes():
    out = [nm.NavMesh(1)]
    if nm.detour_lib() is not None:
        out.append(nm.DetourMesh(1, mmap_params()))
    for m in out:
        m.add_tile("0013232.mmtile", build_tile(l_corridor()))
    return out


def test_tile_file_matches_the_core_naming():
    # Uldaman entrance: gx from y, gy from x (GridMap "reversed"), file (map, gy, gx)
    assert nm.tile_file(70, -226.8, 49.1) == "0703231.mmtile"
    assert nm.tile_file(70, 10.0, 10.0) == "0703131.mmtile"


def test_parse_tile_reads_polys_in_wow_coordinates():
    raw = nm.parse_tile(build_tile(l_corridor()))
    assert len(raw) == 19
    assert raw[0]["verts"][0] == (10, 10, 0) and raw[0]["flags"] == 1


def test_parse_tile_rejects_other_versions():
    data = bytearray(build_tile(l_corridor()))
    data[8] = 5  # mmapVersion
    with pytest.raises(ValueError):
        nm.parse_tile(bytes(data))


@pytest.mark.parametrize("mesh", meshes(), ids=lambda m: m.engine)
def test_path_turns_at_the_inner_corner_and_stays_on_the_mesh(mesh):
    polys = l_corridor()
    path = mesh.find_path(START, GOAL, margin=0.6)
    assert len(path) == 3, path
    assert math.dist(path[0][:2], START[:2]) < 0.1 and math.dist(path[-1][:2], GOAL[:2]) < 0.1
    corner = path[1]
    assert 0.2 < math.dist(corner[:2], CORNER) < 1.2  # hugs the corner, kept off it by the margin
    assert corner[0] > CORNER[0] and corner[1] < CORNER[1]  # on the outside of the turn
    for a, b in zip(path, path[1:]):
        assert segment_on_mesh(polys, a, b)


@pytest.mark.parametrize("mesh", meshes(), ids=lambda m: m.engine)
def test_off_mesh_goal_is_no_path(mesh):
    with pytest.raises(nm.NoPath):
        mesh.find_path(START, (200, 200, 0))


def test_separate_islands_have_no_route():
    island = [[(100, 100, 0), (104, 100, 0), (104, 104, 0), (100, 104, 0)]]
    for mesh in [nm.NavMesh(1)] + ([nm.DetourMesh(1, mmap_params())] if nm.detour_lib() else []):
        mesh.add_tile("t.mmtile", build_tile(l_corridor() + island))
        with pytest.raises(nm.NoPath):
            mesh.find_path(START, (102, 102, 0))


def test_funnel_straight_corridor_is_one_segment():
    portals = [((0, 0, 0), (0, 0, 0)), ((5, 1, 0), (5, -1, 0)), ((10, 1, 0), (10, -1, 0)), ((15, 0, 0), (15, 0, 0))]
    assert nm.funnel(portals) == [(0, 0, 0), (15, 0, 0)]


class FakeSource:
    def __init__(self, files):
        self.files, self.reads, self.mtime = files, [], 1

    def listing(self, map_id):
        return {n: (len(d), self.mtime) for n, d in self.files.items() if n.startswith(f"{map_id:03d}")}

    def read(self, name):
        self.reads.append(name)
        return self.files[name]


def test_tile_cache_copies_once_and_refreshes_when_the_server_file_changes(tmp_path):
    files = {"001.mmap": mmap_params(), "0013232.mmtile": build_tile(l_corridor())}
    src = FakeSource(files)
    cache = nm.TileCache(str(tmp_path), src, "gameserver")
    m = cache.mesh_for(1, [START, GOAL], engine="python")
    assert m.engine == "python" and m.tiles == {"0013232.mmtile"}
    cache2 = nm.TileCache(str(tmp_path), src, "gameserver")
    cache2.mesh_for(1, [START, GOAL], engine="python")
    assert src.reads == ["0013232.mmtile"]  # second time from the cache
    files["0013232.mmtile"], src.mtime = build_tile(l_corridor(z=1.0)), 2  # rebuilt on the server
    nm.TileCache(str(tmp_path), src, "gameserver").mesh_for(1, [START, GOAL], engine="python")
    assert src.reads == ["0013232.mmtile", "0013232.mmtile"]


def test_tile_cache_picks_only_tiles_near_the_path_on_big_maps(tmp_path):
    files = {f"000{r:02d}{c:02d}.mmtile": b"x" for r in range(40, 60) for c in range(20, 40)}
    cache = nm.TileCache(str(tmp_path), FakeSource(files), "s")
    got = []
    class Rec(nm.NavMesh):
        def add_tile(self, name, data):
            got.append(name)
            self.tiles.add(name)
    cache.mesh_for(0, [(-8913, 554, 94), (-8900, 560, 94)], mesh=Rec(0))
    assert len(got) == 9 and nm.tile_file(0, -8913, 554) in got


@pytest.mark.skipif(nm.detour_lib() is None, reason="Detour library not built (tools/build_navdetour.sh so)")
def test_detour_checks_header_versions():
    m = nm.DetourMesh(1, mmap_params())
    data = bytearray(build_tile(l_corridor()))
    data[4] = 6  # dtVersion
    with pytest.raises(ValueError, match="version"):
        m.add_tile("x.mmtile", bytes(data))


REAL = os.environ.get("WOWHARNESS_MMAPS")  # a folder with 070.mmap + 070*.mmtile copied from a server


@pytest.mark.skipif(not REAL, reason="set WOWHARNESS_MMAPS to a folder with the Uldaman (070) tiles")
def test_real_uldaman_tiles_both_engines_agree():
    import glob
    lens = {}
    for eng in ("detour", "python"):
        m = nm.make_mesh(70, lambda: open(os.path.join(REAL, "070.mmap"), "rb").read(), eng)
        for f in sorted(glob.glob(os.path.join(REAL, "070*.mmtile"))):
            m.add_tile(os.path.basename(f), open(f, "rb").read())
        p = m.find_path((-226.8, 49.1, -46.0), (-275, 80, -48.7))
        lens[eng] = nm.path_length(p)
    assert abs(lens["detour"] - lens["python"]) < 3 and 60 < lens["detour"] < 75


def test_ssh_tiles_lists_and_reads_read_only():
    calls = []

    class P:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, b"" if isinstance(out, bytes) else "", 0

    def run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        if "stat" in cmd[-1]:
            return P("DIR /home/user/tortoise-server/data/mmaps\n070.mmap 28 100\n0703231.mmtile 595752 200\nnoise\n")
        return P(b"DATA")

    s = nm.SshTiles(["-i", "k", "pk@host"], "~/tortoise-wow-server/data/mmaps", "tortoise-mangosd", run=run)
    assert s.listing(70) == {"070.mmap": (28, 100), "0703231.mmtile": (595752, 200)}
    assert s.read("0703231.mmtile") == b"DATA"
    assert calls[0][:5] == ["ssh", "-o", "BatchMode=yes", "-i", "k"]
    assert calls[0][-1].startswith("for d in ~/tortoise-wow-server/data/mmaps $(docker inspect")
    assert "tortoise-mangosd" in calls[0][-1] and "stat -c" in calls[0][-1]
    assert calls[1][-1] == "cat /home/user/tortoise-server/data/mmaps/0703231.mmtile"  # where it was found
    with pytest.raises(ValueError):
        s.read("../etc/passwd")

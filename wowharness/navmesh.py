"""The server's own navmesh (the Detour mmaps the bots path on), read offline: tiles, a polygon graph, A* over
polygons and a funnel (string-pull) so the waypoints hug the corridor instead of zig-zagging between polygon
centres.

Tiles come from the server's `data/mmaps` folder (read-only, over the same ssh the harness uses for the log) and
are kept in a local cache; only the tiles a path needs are copied. File format: MmapTileHeader (20 bytes) then a
Detour tile (dtMeshHeader, verts, polys, ...). Detour stores (y, z, x) per vertex in WoW terms.
"""
import heapq
import json
import math
import os
import re
import struct
import subprocess
from dataclasses import dataclass, field

TILE = 533.3333333  # yards per grid tile (GridMap.h SIZE_OF_GRIDS)
MMAP_MAGIC, MMAP_VERSION = 0x4D4D4150, 6
DT_MAGIC = (ord("D") << 24) | (ord("N") << 16) | (ord("A") << 8) | ord("V")
DT_EXT_LINK = 0x8000
DT_POLYTYPE_OFFMESH = 1
# MoveMapSharedDefines.h NavTerrain: walkable for a player on foot = ground or water (swims);
# magma, slime and steep slopes (above player climb) are left out.
NAV_GROUND, NAV_MAGMA, NAV_SLIME, NAV_WATER, NAV_STEEP = 0x01, 0x02, 0x04, 0x08, 0x10
WALKABLE, AVOID = NAV_GROUND | NAV_WATER, NAV_MAGMA | NAV_SLIME | NAV_STEEP
WATER_COST = 2.0
DEFAULT_REMOTE_DIR = "~/tortoise-wow-server/data/mmaps"


class NoPath(Exception):
    pass


def tile_file(map_id: int, x: float, y: float) -> str:
    """The tile file holding a WoW position: the core names it (map, gy, gx) with gx from y and gy from x
    ("it's reversed", GridMap.cpp; MoveMap.cpp loads %03i%02i%02i.mmtile)."""
    gx, gy = int(32 - y / TILE), int(32 - x / TILE)
    return f"{map_id:03d}{gy:02d}{gx:02d}.mmtile"


def tile_rc(name: str) -> tuple[int, int]:
    return int(name[3:5]), int(name[5:7])


@dataclass
class Poly:
    verts: list  # [(x, y, z)] WoW coordinates
    flags: int
    area: int
    tile: str
    links: list = field(default_factory=list)  # [(other poly index, (edge point a, edge point b))]

    @property
    def centre(self):
        n = len(self.verts)
        return tuple(sum(v[k] for v in self.verts) / n for k in range(3))


def parse_tile(data: bytes, name: str = "?") -> list[dict]:
    """Raw polygons of one .mmtile: verts, neighbour codes, flags, area, type."""
    if len(data) < 120:
        raise ValueError(f"{name}: {len(data)} bytes is not an mmtile")
    magic, dtver, mver, size, _liq = struct.unpack_from("<5I", data, 0)
    if magic != MMAP_MAGIC or mver != MMAP_VERSION:
        raise ValueError(f"{name}: bad mmap header (magic {magic:#x}, version {mver})")
    h = data[20:]
    (dmagic, dver, _tx, _ty, _layer, _uid, pc, vc, _mlc, _dmc, _dvc, _dtc, _bvc, _omc, _omb) = struct.unpack_from("<5iI9i", h, 0)
    if dmagic != DT_MAGIC:
        raise ValueError(f"{name}: bad Detour magic {dmagic:#x}")
    off = 100
    verts = []
    for i in range(vc):
        ry, rz, rx = struct.unpack_from("<3f", h, off + 12 * i)
        verts.append((rx, ry, rz))
    off += 12 * vc
    out = []
    for i in range(pc):
        base = off + 32 * i
        vv = struct.unpack_from("<6H", h, base + 4)
        nn = struct.unpack_from("<6H", h, base + 16)
        flags, nvc, at = struct.unpack_from("<HBB", h, base + 28)
        out.append({"verts": [verts[vv[k]] for k in range(nvc)], "nei": list(nn[:nvc]),
                    "flags": flags, "area": at & 0x3F, "type": at >> 6})
    return out


# -- 2D geometry ------------------------------------------------------------------------------------------

def cross(o, a, b) -> float:
    """> 0 when b is left of o->a (x north, y west: counter-clockwise, like WoW orientation)."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def point_in_poly(x: float, y: float, verts) -> bool:
    ins, n = False, len(verts)
    for k in range(n):
        x1, y1 = verts[k][0], verts[k][1]
        x2, y2 = verts[(k + 1) % n][0], verts[(k + 1) % n][1]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            ins = not ins
    return ins


def height_in_poly(x: float, y: float, verts) -> float:
    """Z of the polygon's plane at (x, y): barycentric in the fan triangle that holds the point."""
    a = verts[0]
    for k in range(1, len(verts) - 1):
        b, c = verts[k], verts[k + 1]
        d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(d) < 1e-9:
            continue
        l1 = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / d
        l2 = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / d
        l3 = 1 - l1 - l2
        if min(l1, l2, l3) >= -1e-6:
            return l1 * a[2] + l2 * b[2] + l3 * c[2]
    return sum(v[2] for v in verts) / len(verts)


def closest_on_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if L < 1e-12 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return (a[0] + t * dx, a[1] + t * dy, a[2] + t * (b[2] - a[2]))


def dist2d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


# -- the mesh ---------------------------------------------------------------------------------------------

class NavMesh:
    """The offline fallback: pure Python polygon graph, A* and funnel (no native library needed)."""
    engine = "python"
    CELL = 16.0  # spatial index cell, yards

    def __init__(self, map_id: int = 0):
        self.map_id = map_id
        self.polys: list[Poly] = []
        self.tiles: set[str] = set()
        self._grid: dict[tuple[int, int], list[int]] = {}
        self._ext: dict[tuple, list] = {}  # border line key -> [(poly, a, b)] of edges that leave their tile

    # building
    def add_tile(self, name: str, data: bytes) -> int:
        if name in self.tiles:
            return 0
        raw = parse_tile(data, name)
        base = len(self.polys)
        self.tiles.add(name)
        for r in raw:
            # off-mesh connections (jumps) are left out: a player at the keyboard cannot take them reliably
            self.polys.append(Poly(r["verts"], r["flags"] if r["type"] != DT_POLYTYPE_OFFMESH else 0, r["area"], name))
        for i, r in enumerate(raw):
            pi = base + i
            n = len(r["verts"])
            for k, nb in enumerate(r["nei"]):
                if nb == 0:
                    continue
                a, b = r["verts"][k], r["verts"][(k + 1) % n]
                if nb & DT_EXT_LINK:
                    self._link_external(pi, a, b)
                elif base + nb - 1 > pi:  # each inner pair once
                    j = base + nb - 1
                    self.polys[pi].links.append((j, (a, b)))
                    self.polys[j].links.append((pi, (a, b)))
            self._index(pi)
        return len(raw)

    def _index(self, pi: int) -> None:
        vs = self.polys[pi].verts
        x0, x1 = min(v[0] for v in vs), max(v[0] for v in vs)
        y0, y1 = min(v[1] for v in vs), max(v[1] for v in vs)
        for cx in range(math.floor(x0 / self.CELL), math.floor(x1 / self.CELL) + 1):
            for cy in range(math.floor(y0 / self.CELL), math.floor(y1 / self.CELL) + 1):
                self._grid.setdefault((cx, cy), []).append(pi)

    def _link_external(self, pi: int, a, b) -> None:
        """Tile-border edges lie on x = const or y = const; match the other tile's edges on the same line
        that overlap this one (and sit at about the same height)."""
        if abs(a[0] - b[0]) < 0.01:
            axis, lo, hi = 0, *sorted((a[1], b[1]))
        elif abs(a[1] - b[1]) < 0.01:
            axis, lo, hi = 1, *sorted((a[0], b[0]))
        else:
            return
        line = round(a[axis] * 10)
        zmid = (a[2] + b[2]) / 2
        for key in ((axis, line - 1), (axis, line), (axis, line + 1)):
            for pj, c, d, lo2, hi2, z2 in self._ext.get(key, []):
                if self.polys[pj].tile == self.polys[pi].tile:
                    continue
                o0, o1 = max(lo, lo2), min(hi, hi2)
                if o1 - o0 > 0.1 and abs(zmid - z2) < 3:
                    # the shared part of the two edges is the portal
                    def at(t, p, q):
                        u = (t - (p[1 - axis])) / ((q[1 - axis] - p[1 - axis]) or 1e-9)
                        return tuple(p[k] + u * (q[k] - p[k]) for k in range(3))
                    e = (at(o0, a, b), at(o1, a, b))
                    self.polys[pi].links.append((pj, e))
                    self.polys[pj].links.append((pi, e))
        self._ext.setdefault((axis, line), []).append((pi, a, b, lo, hi, zmid))

    # queries
    def walkable(self, pi: int) -> bool:
        f = self.polys[pi].flags
        return bool(f & WALKABLE) and not f & AVOID

    def locate(self, x: float, y: float, z: float, max_dist: float = 8.0, max_dz: float = 6.0):
        """(poly index, point on the mesh) nearest to a position, or None. Prefers the polygon right under the
        point at the closest height (stacked floors), else the nearest edge within max_dist."""
        best, cand = None, set()
        r = int(math.ceil(max_dist / self.CELL))
        cx, cy = math.floor(x / self.CELL), math.floor(y / self.CELL)
        for i in range(cx - r, cx + r + 1):
            for j in range(cy - r, cy + r + 1):
                cand.update(self._grid.get((i, j), ()))
        for pi in cand:
            if not self.walkable(pi):
                continue
            vs = self.polys[pi].verts
            if point_in_poly(x, y, vs):
                pz = height_in_poly(x, y, vs)
                dz = abs(pz - z)
                if dz <= max_dz and (best is None or (0.0, dz) < best[0]):
                    best = ((0.0, dz), pi, (x, y, pz))
        if best:
            return best[1], best[2]
        for pi in cand:
            if not self.walkable(pi):
                continue
            vs = self.polys[pi].verts
            for k in range(len(vs)):
                q = closest_on_segment((x, y, z), vs[k], vs[(k + 1) % len(vs)])
                d, dz = dist2d(q, (x, y)), abs(q[2] - z)
                if d <= max_dist and dz <= max_dz and (best is None or (d, dz) < best[0]):
                    best = ((d, dz), pi, q)
        return (best[1], best[2]) if best else None

    def _on_mesh_2d(self, x: float, y: float) -> bool:
        for pi in self._grid.get((math.floor(x / self.CELL), math.floor(y / self.CELL)), ()):
            if self.walkable(pi) and point_in_poly(x, y, self.polys[pi].verts):
                return True
        return False

    def clear(self, a, b, step: float = 0.25) -> bool:
        """A straight walk from a to b stays on walkable polygons (sampled every `step` yards, 2D)."""
        n = max(1, int(dist2d(a, b) / step))
        return all(self._on_mesh_2d(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1))

    def near(self, points, r: float) -> set[int]:
        """Polygons that come within r yards (2D) of any of the points."""
        out = set()
        for p in points:
            c = int(math.ceil(r / self.CELL))
            cx, cy = math.floor(p[0] / self.CELL), math.floor(p[1] / self.CELL)
            for i in range(cx - c, cx + c + 1):
                for j in range(cy - c, cy + c + 1):
                    for pi in self._grid.get((i, j), ()):
                        if pi in out:
                            continue
                        vs = self.polys[pi].verts
                        if point_in_poly(p[0], p[1], vs) or any(
                                dist2d(closest_on_segment(p, vs[k], vs[(k + 1) % len(vs)]), p) <= r
                                for k in range(len(vs))):
                            out.add(pi)
        return out

    def corridor(self, s: int, t: int, start, goal, skip=frozenset()) -> list[tuple[int, tuple]]:
        """A* over polygons; a node's position is where the path enters it (the portal midpoint), as Detour
        does. Returns [(poly, portal into it)], the first with portal None. Polygons in `skip` are not entered."""
        def mid(e):
            return tuple((e[0][k] + e[1][k]) / 2 for k in range(3))
        g = {s: 0.0}
        pos = {s: start}
        prev: dict[int, tuple[int, tuple]] = {}
        pq = [(dist2d(start, goal), 0, s)]
        n = 0
        while pq:
            _, _, u = heapq.heappop(pq)
            if u == t:
                break
            for w, e in self.polys[u].links:
                if not self.walkable(w) or w in skip:
                    continue
                p = goal if w == t else mid(e)
                cost = math.dist(pos[u], p) * (WATER_COST if self.polys[w].flags & NAV_WATER and not
                                               self.polys[w].flags & NAV_GROUND else 1.0)
                if g[u] + cost < g.get(w, math.inf):
                    g[w], pos[w], prev[w] = g[u] + cost, p, (u, e)
                    n += 1
                    heapq.heappush(pq, (g[w] + 0.999 * dist2d(p, goal), n, w))
        if t not in g:
            if skip:
                raise NoPath(f"no navmesh route that keeps clear of the {len(skip)} polygons round the avoid points")
            raise NoPath("no navmesh route between start and goal (different islands of the mesh)")
        out = [(t, prev[t][1] if t in prev else None)]
        while out[-1][0] != s:
            u, _ = prev[out[-1][0]]
            out.append((u, prev[u][1] if u in prev else None))
        return list(reversed(out))

    def find_path(self, start, goal, margin: float = 0.6, snap: float = 8.0, avoid=(), avoid_r: float = 3.0) -> list[tuple]:
        """Waypoints from start to goal (WoW x, y, z): A* corridor, then the funnel. `margin` keeps the path
        that far from portal ends (corners), so a real client's collision does not catch on them. `avoid`:
        points (a closed door, an object the mesh does not know) whose polygons within avoid_r are not entered
        (the player's own polygon still is, so a walk can start next to one)."""
        a = self.locate(*start, max_dist=snap)
        if a is None:
            raise NoPath(f"start {fmt(start)} is not on the navmesh (map {self.map_id}, tiles {sorted(self.tiles)})")
        b = self.locate(*goal, max_dist=snap)
        if b is None:
            raise NoPath(f"goal {fmt(goal)} is not on the navmesh (map {self.map_id}, tiles {sorted(self.tiles)})")
        (s, sp), (t, tp) = a, b
        skip = self.near(avoid, avoid_r) - {s} if avoid else frozenset()
        if t in skip:
            raise NoPath(f"goal {fmt(goal)} is within {avoid_r:g} yd of an avoid point")
        if s == t:
            return [sp, tp]
        cor = self.corridor(s, t, sp, tp, skip)
        portals = [(sp, sp)]
        for k in range(1, len(cor)):
            u, e = cor[k - 1][0], cor[k][1]
            portals.append(orient_portal(self.polys[u].centre, self.polys[cor[k][0]].centre, e, margin))
        portals.append((tp, tp))
        return push_corners(funnel(portals), margin, self._on_mesh_2d)


def orient_portal(from_c, to_c, edge, margin: float):
    """(left, right) of a portal seen walking from one polygon centre to the next, pulled in by margin."""
    p, q = edge
    m = tuple((p[k] + q[k]) / 2 for k in range(3))
    d = (to_c[0] - from_c[0], to_c[1] - from_c[1])
    left, right = (p, q) if d[0] * (p[1] - m[1]) - d[1] * (p[0] - m[0]) > 0 else (q, p)
    w = dist2d(left, right)
    if w <= 2 * margin:
        return m, m
    f = margin / w
    lt = tuple(left[k] + f * (right[k] - left[k]) for k in range(3))
    rt = tuple(right[k] + f * (left[k] - right[k]) for k in range(3))
    return lt, rt


def funnel(portals: list[tuple]) -> list[tuple]:
    """Simple stupid funnel algorithm (Mononen) on (left, right) portals; the first and last are the start
    and goal points. Corners are taken at portal ends, so each waypoint keeps its 3D height."""
    pts = [portals[0][0]]
    apex = left = right = portals[0][0]
    ai = li = ri = 0
    i = 1
    while i < len(portals):
        lft, rgt = portals[i]
        # tighten the right side (right must stay right of apex->left)
        if cross(apex, right, rgt) >= 0:
            if apex == right or cross(apex, left, rgt) < 0:
                right, ri = rgt, i
            else:
                pts.append(left)
                apex, ai = left, li
                left = right = apex
                li = ri = ai
                i = ai + 1
                continue
        if cross(apex, left, lft) <= 0:
            if apex == left or cross(apex, right, lft) > 0:
                left, li = lft, i
            else:
                pts.append(right)
                apex, ai = right, ri
                left = right = apex
                li = ri = ai
                i = ai + 1
                continue
        i += 1
    if pts[-1] != portals[-1][0]:
        pts.append(portals[-1][0])
    # drop repeats (an apex can equal the goal), and merge inner corners closer than a yard: with pulled-in
    # portals one wall corner can show up as two apexes, one per portal touching it
    out = [pts[0]]
    for p in pts[1:]:
        if math.dist(p, out[-1]) > 0.05:
            out.append(p)
    k = 1
    while k < len(out) - 2:
        if dist2d(out[k], out[k + 1]) < 1.0:
            out[k:k + 2] = [tuple((out[k][j] + out[k + 1][j]) / 2 for j in range(3))]
        else:
            k += 1
    return out


def push_corners(pts, margin: float, on_mesh) -> list:
    """Move each inner corner up to `margin` away from the wall corner it wraps round (along the outside of the
    turn), as the Detour engine does, while the point stays on the mesh."""
    out = list(pts)
    for i in range(1, len(out) - 1):
        a, p, b = out[i - 1], out[i], out[i + 1]
        il, ol = dist2d(a, p), dist2d(p, b)
        if il < 1e-3 or ol < 1e-3:
            continue
        dx = (p[0] - a[0]) / il - (b[0] - p[0]) / ol
        dy = (p[1] - a[1]) / il - (b[1] - p[1]) / ol
        dl = math.hypot(dx, dy)
        if dl < 1e-3:
            continue
        m = margin
        while m >= margin / 4:
            q = (p[0] + dx / dl * m, p[1] + dy / dl * m, p[2])
            if on_mesh(q[0], q[1]):
                out[i] = q
                break
            m /= 2
    return out


def path_length(points) -> float:
    return sum(math.dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def fmt(p) -> str:
    return "(" + ", ".join(f"{c:.1f}" for c in p) + ")"


# -- tiles from the server --------------------------------------------------------------------------------

class SshTiles:
    """Lists and reads a server's mmaps over ssh (read-only: stat and cat)."""

    def __init__(self, ssh: tuple | list, remote_dir: str = DEFAULT_REMOTE_DIR, container: str = "",
                 run=subprocess.run):
        self.ssh, self.dir, self.container, self._run = list(ssh), remote_dir, container, run
        self.found_dir = None

    def _ssh(self, cmd: str, text: bool):
        p = self._run(["ssh", "-o", "BatchMode=yes", *self.ssh, cmd], capture_output=True, text=text, timeout=60)
        if p.returncode != 0 and not p.stdout:
            err = p.stderr if text else p.stderr.decode("utf-8", "replace")
            raise OSError(f"ssh {' '.join(self.ssh[-1:])} {cmd!r} failed: {err.strip()[:200]}")
        return p.stdout

    def listing(self, map_id: int) -> dict[str, tuple[int, int]]:
        """The map's files with (size, mtime). Looks in remote_dir, else in the server container's mounted
        folders (a server laid out differently, e.g. the VM's ~/t136/tortoise-server/data -> /opt/tortoise/data)."""
        m = f"{map_id:03d}"
        mounts = (f" $(docker inspect -f '{{{{range .Mounts}}}}{{{{.Source}}}} {{{{end}}}}' {self.container})"
                  if self.container else "")
        cmd = (f"for d in {self.found_dir or self.dir}{mounts}; do [ -d \"$d/mmaps\" ] && d=\"$d/mmaps\"; "
               f"if [ -f \"$d/{m}.mmap\" ]; then cd \"$d\" && echo \"DIR $(pwd)\" && "
               f"stat -c '%n %s %Y' {m}.mmap {m}*.mmtile; break; fi; done 2>/dev/null")
        out = self._ssh(cmd, True)
        files = {}
        for line in out.splitlines():
            if line.startswith("DIR "):
                self.found_dir = line[4:].strip()
                continue
            m = re.match(r"(\d{7}\.mmtile|\d{3}\.mmap) (\d+) (\d+)$", line.strip())
            if m:
                files[m.group(1)] = (int(m.group(2)), int(m.group(3)))
        return files

    def read(self, name: str) -> bytes:
        if not re.fullmatch(r"\d{7}\.mmtile|\d{3}\.mmap", name):
            raise ValueError(f"not a tile name: {name}")
        return self._ssh(f"cat {self.found_dir or self.dir}/{name}", False)


class TileCache:
    """Local copy of the tiles a path needs: <cache_dir>/<server>/<file>, refreshed when the server's file
    changes (size or mtime). `source` has listing(map_id) and read(name) (SshTiles, or a fake in tests)."""

    def __init__(self, cache_dir: str, source, server_key: str = "server"):
        self.dir = os.path.join(cache_dir, re.sub(r"[^A-Za-z0-9_.-]", "_", server_key))
        self.source = source
        self._listings: dict[int, dict] = {}

    def _index_path(self):
        return os.path.join(self.dir, "index.json")

    def _index(self) -> dict:
        try:
            with open(self._index_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def listing(self, map_id: int) -> dict:
        if map_id not in self._listings:
            self._listings[map_id] = self.source.listing(map_id)
        return self._listings[map_id]

    def get(self, name: str, stamp) -> bytes:
        os.makedirs(self.dir, exist_ok=True)
        path, idx = os.path.join(self.dir, name), self._index()
        if os.path.exists(path) and idx.get(name) == list(stamp):
            with open(path, "rb") as f:
                return f.read()
        data = self.source.read(name)
        if stamp and len(data) != stamp[0]:
            raise OSError(f"{name}: got {len(data)} bytes, the server lists {stamp[0]}")
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)
        idx[name] = list(stamp)
        with open(self._index_path(), "w", encoding="utf-8") as f:
            json.dump(idx, f)
        return data

    def mesh_for(self, map_id: int, points, mesh=None, margin_tiles: int = 1, all_below: int = 16,
                 engine: str = "auto"):
        """A mesh with the tiles around the given points (all of the map's tiles when it has few, like a
        dungeon). Adds to `mesh` if given, so a later, longer path only fetches what is new. engine: "detour"
        (the real Detour, as the server), "python" (the offline fallback) or "auto" (Detour when it loads)."""
        files = self.listing(map_id)
        tiles = {n for n in files if n.endswith(".mmtile")}
        if not tiles:
            raise NoPath(f"the server has no mmaps for map {map_id}")
        if mesh is None:
            mesh = make_mesh(map_id, lambda: self.get(f"{map_id:03d}.mmap", files[f"{map_id:03d}.mmap"])
                             if f"{map_id:03d}.mmap" in files else None, engine)
        if len(tiles) <= all_below:
            want = set(tiles)
        else:
            rcs = [tile_rc(tile_file(map_id, p[0], p[1])) for p in points]
            r0, r1 = min(r for r, _ in rcs) - margin_tiles, max(r for r, _ in rcs) + margin_tiles
            c0, c1 = min(c for _, c in rcs) - margin_tiles, max(c for _, c in rcs) + margin_tiles
            want = {f"{map_id:03d}{r:02d}{c:02d}.mmtile" for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)}
            want &= tiles
        for name in sorted(want - mesh.tiles):
            mesh.add_tile(name, self.get(name, files[name]))
        return mesh


# -- the real Detour (native/navdetour.cpp over the core's own Detour) -----------------------------------

_LIB = None


def detour_lib():
    """The Detour library for this platform (wowharness/native), or None when it is missing or the wrong build."""
    global _LIB
    if _LIB is None:
        import ctypes
        import sys
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native")
        path = os.path.join(here, "navdetour.dll" if sys.platform == "win32" else "libnavdetour.so")
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            _LIB = False
            return None
        f, u16, vp, i = ctypes.POINTER(ctypes.c_float), ctypes.c_ushort, ctypes.c_void_p, ctypes.c_int
        lib.nd_version.restype = i
        lib.nd_create.restype, lib.nd_create.argtypes = vp, [ctypes.c_char_p, i]
        lib.nd_destroy.argtypes = [vp]
        lib.nd_add_tile.restype, lib.nd_add_tile.argtypes = i, [vp, ctypes.c_char_p, i]
        lib.nd_nearest.restype, lib.nd_nearest.argtypes = i, [vp, f, u16, u16, f]
        lib.nd_clear.restype, lib.nd_clear.argtypes = i, [vp, f, f, u16, u16]
        lib.nd_find_path.restype = i
        lib.nd_find_path.argtypes = [vp, f, f, u16, u16, ctypes.c_float, f, i, ctypes.POINTER(i)]
        # DT_NAVMESH_VERSION 7 with the core's 64-bit poly refs: tiles do not load into another build
        _LIB = lib if lib.nd_version() == 708 else False
    return _LIB or None


class DetourMesh:
    """Detour navmesh + query: findPath and findStraightPath like the server's PathFinder (straight-path
    mode), then corners moved `margin` yards off the walls where the mesh allows."""
    engine = "detour"
    _ERR = {-1: "bad tile header", -2: "mmap/Detour version mismatch", -3: "truncated tile", -4: "Detour refused the tile"}

    def __init__(self, map_id: int, mmap_params: bytes):
        import ctypes
        self._ct = ctypes
        self.lib = detour_lib()
        if self.lib is None:
            raise OSError("Detour library (wowharness/native) missing or not the core's build; run tools/build_navdetour.sh")
        self.map_id, self.tiles = map_id, set()
        self.h = self.lib.nd_create(mmap_params, len(mmap_params))
        if not self.h:
            raise ValueError(f"map {map_id}: Detour rejected the {len(mmap_params)}-byte .mmap params")

    def __del__(self):
        if getattr(self, "h", None) and self.lib:
            self.lib.nd_destroy(self.h)
            self.h = None

    def add_tile(self, name: str, data: bytes) -> int:
        if name in self.tiles:
            return 0
        rc = self.lib.nd_add_tile(self.h, data, len(data))
        if rc != 0:
            raise ValueError(f"{name}: {self._ERR.get(rc, rc)}")
        self.tiles.add(name)
        return 1

    def _vec(self, p):
        return (self._ct.c_float * 3)(*p)

    def locate(self, x, y, z, **_):
        out = (self._ct.c_float * 3)()
        if self.lib.nd_nearest(self.h, self._vec((x, y, z)), WALKABLE, AVOID, out) != 0:
            return None
        return None, tuple(out)

    def clear(self, a, b) -> bool:
        """A straight walk from a to b stays on the mesh (Detour raycast)."""
        return self.lib.nd_clear(self.h, self._vec(a), self._vec(b), WALKABLE, AVOID) == 1

    def find_path(self, start, goal, margin: float = 0.6, snap: float = 5.0, allow_partial: bool = False,
                  avoid=(), avoid_r: float = 3.0):
        if avoid:
            raise NoPath("the Detour engine cannot route round avoid points (it has no per-call polygon filter "
                         "here): use engine='python'")
        n = self._ct.c_int(0)
        out = (self._ct.c_float * (256 * 3))()
        rc = self.lib.nd_find_path(self.h, self._vec(start), self._vec(goal), WALKABLE, AVOID, margin, out, 256,
                                   self._ct.byref(n))
        if rc == -1:
            raise NoPath(f"start {fmt(start)} is not on the navmesh (map {self.map_id}, tiles {sorted(self.tiles)})")
        if rc == -2:
            raise NoPath(f"goal {fmt(goal)} is not on the navmesh (map {self.map_id}, tiles {sorted(self.tiles)})")
        if rc < 0 or n.value < 1:
            raise NoPath(f"Detour found no path from {fmt(start)} to {fmt(goal)}")
        pts = [tuple(round(out[3 * k + j], 2) for j in range(3)) for k in range(n.value)]
        if rc == 1 and not allow_partial:
            raise NoPath(f"no navmesh route to {fmt(goal)}: the nearest reachable point is {fmt(pts[-1])}, "
                         f"{dist2d(pts[-1], goal):.1f} yd away (an island of the mesh)")
        return pts


def make_mesh(map_id: int, read_params, engine: str = "auto"):
    """DetourMesh when asked for (or, on auto, when the library loads and the .mmap is there), else NavMesh."""
    if engine in ("auto", "detour") and (engine == "detour" or detour_lib() is not None):
        params = read_params()
        if params is not None:
            return DetourMesh(map_id, params)
        if engine == "detour":
            raise NoPath(f"the server has no {map_id:03d}.mmap")
    return NavMesh(map_id)

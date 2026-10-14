"""Synthetic .mmtile/.mmap files in the server's format (MmapTileHeader v6 + Detour v7 tile, 64-bit poly refs),
so the navmesh tests run without server data. Polygons are given in WoW (x, y, z), counter-clockwise in (x, y)
like the real tiles."""
import struct

TILE = 533.3333333
DT_MAGIC = (ord("D") << 24) | (ord("N") << 16) | (ord("A") << 8) | ord("V")


def mmap_params() -> bytes:
    """dtNavMeshParams: orig (Detour y,z,x order), tile width/height, max tiles, max polys."""
    return struct.pack("<5f2i", 0.0, -500.0, 0.0, TILE, TILE, 4, 1 << 16)


def build_tile(polys, flags=None, tx=0, ty=0) -> bytes:
    flags = flags or [1] * len(polys)
    verts, index = [], {}
    pidx = []
    for poly in polys:
        ids = []
        for v in poly:
            key = tuple(round(c, 4) for c in v)
            if key not in index:
                index[key] = len(verts)
                verts.append(key)
            ids.append(index[key])
        pidx.append(ids)
    edges = {}
    for pi, ids in enumerate(pidx):
        for k in range(len(ids)):
            edges.setdefault(frozenset((ids[k], ids[(k + 1) % len(ids)])), []).append(pi)
    neis = []
    for pi, ids in enumerate(pidx):
        row = []
        for k in range(len(ids)):
            other = [p for p in edges[frozenset((ids[k], ids[(k + 1) % len(ids)]))] if p != pi]
            row.append(other[0] + 1 if other else 0)
        neis.append(row)
    nlinks = sum(sum(1 for n in row if n) for row in neis) + 4 * len(polys)
    ntris = sum(len(ids) - 2 for ids in pidx)
    dv = [(v[1], v[2], v[0]) for v in verts]  # WoW (x, y, z) -> Detour (y, z, x)
    bmin = [min(v[k] for v in dv) for k in range(3)]
    bmax = [max(v[k] for v in dv) for k in range(3)]
    head = struct.pack("<5iI9i", DT_MAGIC, 7, tx, ty, 0, 0, len(polys), len(verts), nlinks, len(polys), 0, ntris, 0, 0, 0)
    head += struct.pack("<3f3f3f f", 1.5, 0.3, 1.8, *bmin, *bmax, 0.0)
    assert len(head) == 100
    body = b"".join(struct.pack("<3f", *v) for v in dv)
    for ids, row, fl in zip(pidx, neis, flags):
        vv = ids + [0] * (6 - len(ids))
        nn = row + [0] * (6 - len(row))
        body += struct.pack("<I6H6HHBB", 0xFFFFFFFF, *vv, *nn, fl, len(ids), 1)  # area 1, ground type
    body += b"\0" * (16 * nlinks)  # dtLink with 64-bit refs
    tri_base = 0
    for ids in pidx:
        body += struct.pack("<IIBBxx", 0, tri_base, 0, len(ids) - 2)
        tri_base += len(ids) - 2
    for ids in pidx:  # fan triangles; edge flags mark the polygon's own edges as boundary (2 bits per edge)
        n = len(ids)
        for k in range(1, n - 1):
            edge = (1 if k == 1 else 0) | (1 << 2) | ((1 if k + 1 == n - 1 else 0) << 4)
            body += struct.pack("<4B", 0, k, k + 1, edge)
    data = head + body
    return struct.pack("<5I", 0x4D4D4150, 7, 6, len(data), 0) + data


def l_corridor(z: float = 0.0):
    """4-yd square cells: a row along x from 10 to 50 at y 10..14, then a column up y to 50 at x 46..50.
    Start (12, 12), goal (48, 48); the inner corner is (46, 14)."""
    polys = []
    for x0 in range(10, 50, 4):
        polys.append([(x0, 10, z), (x0 + 4, 10, z), (x0 + 4, 14, z), (x0, 14, z)])
    for y0 in range(14, 50, 4):
        polys.append([(46, y0, z), (50, y0, z), (50, y0 + 4, z), (46, y0 + 4, z)])
    return polys


def ring(z: float = 0.0):
    """4-yd cells round a 40x40 square: bottom row y 10..14, right column x 46..50, top row y 46..50, left
    column x 10..14. Two routes from (12, 12) to (30, 48): up the left column (54 yd, the planner's) or round
    by the right (90 yd)."""
    polys = [[(x0, 10, z), (x0 + 4, 10, z), (x0 + 4, 14, z), (x0, 14, z)] for x0 in range(10, 50, 4)]
    polys += [[(46, y0, z), (50, y0, z), (50, y0 + 4, z), (46, y0 + 4, z)] for y0 in range(14, 50, 4)]
    polys += [[(x0, 46, z), (x0 + 4, 46, z), (x0 + 4, 50, z), (x0, 50, z)] for x0 in range(10, 46, 4)]
    polys += [[(10, y0, z), (14, y0, z), (14, y0 + 4, z), (10, y0 + 4, z)] for y0 in range(14, 46, 4)]
    return polys

// navdetour.cpp - a small C API over Detour for the harness's walk_to (wowharness/navmesh.py, ctypes).
//
// The Detour sources in native/detour are the core's own copy (tortoise-wow dep/recastnavigation, with its
// TrinityCore patch: 64-bit poly refs), because tiles built for 64-bit refs do not load into a 32-bit-ref
// Detour (dtLink size differs). Coordinates in and out are WoW (x, y, z); Detour's are (y, z, x).
// Build: tools/build_navdetour.sh (Linux .so and Windows .dll).
#include <cfloat>
#include <cmath>
#include <cstdint>
#include <cstring>

#include "DetourAlloc.h"
#include "DetourCommon.h"
#include "DetourNavMesh.h"
#include "DetourNavMeshQuery.h"

#if defined(_WIN32)
#define ND_API extern "C" __declspec(dllexport)
#else
#define ND_API extern "C" __attribute__((visibility("default")))
#endif

namespace {
const uint32_t MMAP_MAGIC = 0x4d4d4150;  // 'MMAP'
const uint32_t MMAP_VERSION = 6;         // core MoveMapSharedDefines.h
const int MAX_POLYS = 2048;
const int MAX_NODES = 65535;

struct MmapTileHeader {
    uint32_t mmapMagic, dtVersion, mmapVersion, size, usesLiquids;
};

struct Nav {
    dtNavMesh* mesh = nullptr;
    dtNavMeshQuery* query = nullptr;
};

inline void toDt(const float* w, float* d) { d[0] = w[1]; d[1] = w[2]; d[2] = w[0]; }
inline void toWow(const float* d, float* w) { w[0] = d[2]; w[1] = d[0]; w[2] = d[1]; }

dtPolyRef nearest(Nav* n, const float* dt, const dtQueryFilter& f, float* closest) {
    const float ext[3] = {5.0f, 10.0f, 5.0f};  // as the core's FindWalkPoly, a little more height
    dtPolyRef ref = 0;
    if (dtStatusFailed(n->query->findNearestPoly(dt, ext, &f, &ref, closest)))
        return 0;
    return ref;
}

// True when a walker can go straight from a to b on the mesh (Detour raycast reaches b).
bool clear(Nav* n, const float* a, const float* b, const dtQueryFilter& f) {
    float ca[3];
    dtPolyRef ref = nearest(n, a, f, ca);
    if (!ref)
        return false;
    float t = 0, normal[3];
    dtPolyRef polys[MAX_POLYS];
    int np = 0;
    if (dtStatusFailed(n->query->raycast(ref, ca, b, &f, &t, normal, polys, &np, MAX_POLYS)))
        return false;
    return t >= 1.0f || t == FLT_MAX;
}
}  // namespace

// 100 * DT_NAVMESH_VERSION + sizeof(dtPolyRef): lets the Python side check it has the right build.
ND_API int nd_version() { return 100 * DT_NAVMESH_VERSION + (int)sizeof(dtPolyRef); }

// mmap: the map's NNN.mmap file, which is a raw dtNavMeshParams (28 bytes).
ND_API void* nd_create(const unsigned char* mmap, int size) {
    if (size < (int)sizeof(dtNavMeshParams))
        return nullptr;
    dtNavMeshParams params;
    std::memcpy(&params, mmap, sizeof(params));
    Nav* n = new Nav;
    n->mesh = dtAllocNavMesh();
    n->query = dtAllocNavMeshQuery();
    if (!n->mesh || !n->query || dtStatusFailed(n->mesh->init(&params)) ||
        dtStatusFailed(n->query->init(n->mesh, MAX_NODES))) {
        dtFreeNavMeshQuery(n->query);
        dtFreeNavMesh(n->mesh);
        delete n;
        return nullptr;
    }
    return n;
}

ND_API void nd_destroy(void* h) {
    Nav* n = (Nav*)h;
    if (!n)
        return;
    dtFreeNavMeshQuery(n->query);
    dtFreeNavMesh(n->mesh);
    delete n;
}

// A whole .mmtile file. 0 = added; -1 bad header, -2 mmap/Detour version, -3 truncated, -4 Detour refused.
ND_API int nd_add_tile(void* h, const unsigned char* file, int size) {
    Nav* n = (Nav*)h;
    if (!n || size < (int)sizeof(MmapTileHeader))
        return -1;
    MmapTileHeader hd;
    std::memcpy(&hd, file, sizeof(hd));
    if (hd.mmapMagic != MMAP_MAGIC)
        return -1;
    if (hd.mmapVersion != MMAP_VERSION || hd.dtVersion != (uint32_t)DT_NAVMESH_VERSION)
        return -2;
    if ((int)(sizeof(hd) + hd.size) > size)
        return -3;
    unsigned char* data = (unsigned char*)dtAlloc(hd.size, DT_ALLOC_PERM);
    std::memcpy(data, file + sizeof(hd), hd.size);
    dtTileRef ref = 0;
    if (dtStatusFailed(n->mesh->addTile(data, (int)hd.size, DT_TILE_FREE_DATA, 0, &ref))) {
        dtFree(data);
        return -4;
    }
    return 0;
}

// Nearest walkable point to p (WoW xyz). 0 = found (out set), -1 = none within 5 yd / 10 yd height.
ND_API int nd_nearest(void* h, const float* p, unsigned short include, unsigned short exclude, float* out) {
    Nav* n = (Nav*)h;
    dtQueryFilter f;
    f.setIncludeFlags(include);
    f.setExcludeFlags(exclude);
    float dt[3], c[3];
    toDt(p, dt);
    if (!nearest(n, dt, f, c))
        return -1;
    toWow(c, out);
    return 0;
}

// 1 when a walker can go straight from a to b (WoW xyz) on the mesh, 0 when a wall or a gap is in the way.
ND_API int nd_clear(void* h, const float* a, const float* b, unsigned short include, unsigned short exclude) {
    Nav* n = (Nav*)h;
    dtQueryFilter f;
    f.setIncludeFlags(include);
    f.setExcludeFlags(exclude);
    float da[3], db[3];
    toDt(a, da);
    toDt(b, db);
    return clear(n, da, db, f) ? 1 : 0;
}

// Path from start to end (WoW xyz): findPath + findStraightPath, as the server's PathFinder with straight
// paths. Each inner corner is then moved up to `margin` yards away from the corner it wraps around, when
// the mesh allows it (raycasts to both neighbours stay clear), so a real client does not catch on walls.
// Returns 0 complete, 1 partial (out ends at the reachable point nearest the goal), -1 start off the mesh,
// -2 goal off the mesh, -3 Detour failure. *count = points written to out (3 floats each).
ND_API int nd_find_path(void* h, const float* start, const float* end, unsigned short include,
                        unsigned short exclude, float margin, float* out, int max_points, int* count) {
    Nav* n = (Nav*)h;
    *count = 0;
    dtQueryFilter f;
    f.setIncludeFlags(include);
    f.setExcludeFlags(exclude);
    float s[3], e[3], sc[3], ec[3];
    toDt(start, s);
    toDt(end, e);
    dtPolyRef sref = nearest(n, s, f, sc);
    if (!sref)
        return -1;
    dtPolyRef eref = nearest(n, e, f, ec);
    if (!eref)
        return -2;
    dtPolyRef polys[MAX_POLYS];
    int np = 0;
    dtStatus st = n->query->findPath(sref, eref, sc, ec, &f, polys, &np, MAX_POLYS);
    if (dtStatusFailed(st) || np == 0)
        return -3;
    int result = 0;
    float goal[3];
    dtVcopy(goal, ec);
    if (polys[np - 1] != eref || dtStatusDetail(st, DT_PARTIAL_RESULT)) {
        result = 1;
        n->query->closestPointOnPoly(polys[np - 1], ec, goal, nullptr);
    }
    const int cap = max_points < 256 ? max_points : 256;
    float pts[256 * 3];
    int npts = 0;
    if (dtStatusFailed(n->query->findStraightPath(sc, goal, polys, np, pts, nullptr, nullptr, &npts, cap)) || npts < 1)
        return -3;
    if (margin > 0.0f) {
        for (int i = 1; i + 1 < npts; ++i) {
            float* p = &pts[i * 3];
            const float* a = &pts[(i - 1) * 3];
            const float* b = &pts[(i + 1) * 3];
            // in/out directions on the ground plane (Detour x, z)
            float ix = p[0] - a[0], iz = p[2] - a[2], ox = b[0] - p[0], oz = b[2] - p[2];
            float il = std::sqrt(ix * ix + iz * iz), ol = std::sqrt(ox * ox + oz * oz);
            if (il < 1e-3f || ol < 1e-3f)
                continue;
            ix /= il; iz /= il; ox /= ol; oz /= ol;
            // the corner's obstacle sits inside the turn; (in - out) points away from it
            float dx = ix - ox, dz = iz - oz, dl = std::sqrt(dx * dx + dz * dz);
            if (dl < 1e-3f)
                continue;
            dx /= dl; dz /= dl;
            for (float m = margin; m >= margin / 4; m /= 2) {
                float q[3] = {p[0] + dx * m, p[1], p[2] + dz * m}, qc[3];
                dtPolyRef qref = nearest(n, q, f, qc);
                if (!qref || std::fabs(qc[0] - q[0]) > 0.05f || std::fabs(qc[2] - q[2]) > 0.05f)
                    continue;
                if (clear(n, a, qc, f) && clear(n, qc, b, f)) {
                    dtVcopy(p, qc);
                    break;
                }
            }
        }
    }
    for (int i = 0; i < npts; ++i)
        toWow(&pts[i * 3], &out[i * 3]);
    *count = npts;
    return result;
}

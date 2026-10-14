# native: the Detour path engine for walk_to

`navdetour.cpp` is a small C API (create, add tile, nearest, clear, find path) that `wowharness/navmesh.py`
loads with ctypes. `tools/build_navdetour.sh` builds `wowharness/native/libnavdetour.so` (Linux, g++; not
committed) and `wowharness/native/navdetour.dll` (Windows x64, cross-built with zig from the venv:
`.venv/bin/pip install ziglang`; committed, because the drivers' Windows Python has no compiler). Without
the library, navmesh.py falls back to its pure-Python A* and funnel.

## Third-party code

| What | Source | Licence | How |
| :--- | :--- | :--- | :--- |
| `detour/` (DetourAlloc, Assert, Common, Math, NavMesh, NavMeshQuery, Node, Status) | recastnavigation (Mikko Mononen), as vendored by the core: [tortoise-wow](https://github.com/tortoise-wow/tortoise-wow) `dep/recastnavigation/Detour` at `818b3861f87178a09a3d35aea1321cd39f3a0f12`, with its TrinityCore patch (`DT_POLYREF64`, 12/21/31 salt/tile/poly bits) | zlib, `detour/License.txt` | copied unchanged |

The core's patched Detour is used on purpose: tiles built with 64-bit poly refs do not load into a stock
32-bit-ref Detour (the tile data reserves `dtLink` space, 16 bytes with 64-bit refs, 12 without). The server's
tiles are mmap version 6 / Detour version 7; `nd_add_tile` refuses others, and `nd_version()` (708 = Detour 7,
8-byte refs) lets the Python side refuse a wrong build.

Read for ideas only, no code copied: BloogBot (MIT; its Navigation project uses 32-bit refs and mmap
version 4, so it cannot load these tiles) and WowClassicGrindBot (no licence; steering and stuck handling).

#!/usr/bin/env bash
# tools/build_navdetour.sh — build the Detour path engine for walk_to (wowharness/navmesh.py loads it with ctypes).
#   wowharness/native/libnavdetour.so   Linux (tests), with g++
#   wowharness/native/navdetour.dll     Windows x64 (the drivers' Python), cross-built with zig from the venv:
#                                       .venv/bin/pip install ziglang
set -euo pipefail
cd "$(dirname "$0")/.."
SRC=(native/navdetour.cpp native/detour/Source/*.cpp)
FLAGS=(-O2 -std=c++17 -Inative/detour/Include -shared)
mkdir -p wowharness/native
if [ "${1:-all}" != "dll" ]; then
  g++ "${FLAGS[@]}" -fPIC -fvisibility=hidden -o wowharness/native/libnavdetour.so "${SRC[@]}"
  echo "built wowharness/native/libnavdetour.so"
fi
if [ "${1:-all}" != "so" ]; then
  .venv/bin/python -m ziglang c++ -target x86_64-windows-gnu "${FLAGS[@]}" -static-libstdc++ -s \
    -o wowharness/native/navdetour.dll "${SRC[@]}"
  rm -f wowharness/native/navdetour.lib wowharness/native/navdetour.pdb
  echo "built wowharness/native/navdetour.dll"
fi

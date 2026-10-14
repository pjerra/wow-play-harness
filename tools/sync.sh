#!/usr/bin/env bash
# tools/sync.sh — copy the Linux repo to the Windows run folder (code only).
set -euo pipefail
rsync -a --delete --exclude .git --exclude .venv --exclude __pycache__ --exclude docs \
  "$HOME/wow-play-harness/" /mnt/c/Users/<you>/wow-play-harness/
for client in 1.18.1-7272-Hotfix-2026-04-12 harness-2; do
  ADDONS="/mnt/c/Users/<you>/Clients/$client/Interface/AddOns"
  [ -d "$ADDONS" ] && rsync -a --delete "$HOME/wow-play-harness/addon/HarnessObserver/" "$ADDONS/HarnessObserver/"
done
echo synced

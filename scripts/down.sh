#!/usr/bin/env bash
# Stop only the processes this repo started (tracked by pid file).
set -euo pipefail
source "$(dirname "$0")/lib.sh"

if [[ "$SPLIT_BRAIN_MODE" == "compose" ]]; then
  docker compose -f "$REPO_ROOT/docker-compose.yml" down
  exit 0
fi
stop_pidfile ministack
stop_pidfile aws-emulator

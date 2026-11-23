#!/usr/bin/env bash
# Stop only this probe's instances: AWS Emulator on 9301, MiniStack with GATEWAY_PORT=4601 (+ its Lambda workers).
which=${1:-"emu ms"}
if [[ $which == *emu* ]]; then pkill -TERM -f "^aws-emulator -p 9301" ; fi
if [[ $which == *ms* ]]; then
  for p in $(pgrep -f "bin/ministack$"; pgrep -f "m50venv/bin/python.* -m ministack"); do
    if tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -qx "GATEWAY_PORT=4601"; then
      pkill -TERM -P $p; kill -TERM $p
    fi
  done
fi
sleep 1

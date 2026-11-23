# Shared helpers for the scripts in this folder. Sourced, not executed.
# shellcheck shell=bash

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="$REPO_ROOT/.local"
PID_DIR="$LOCAL_DIR/pids"
LOG_DIR="$LOCAL_DIR/logs"

EMULATOR_PORT="${EMULATOR_PORT:-9297}"
MINISTACK_PORT="${MINISTACK_PORT:-4566}"
EMULATOR_URL="https://127.0.0.1:${EMULATOR_PORT}"
MINISTACK_URL="http://127.0.0.1:${MINISTACK_PORT}"
EMULATOR_CA="$HOME/.aws/emulator/${EMULATOR_PORT}/ca.pem"

# Compose mode is for macOS/Windows hosts. Native is what CI and Linux use.
SPLIT_BRAIN_MODE="${SPLIT_BRAIN_MODE:-native}"

if [[ -t 2 ]]; then _b=$'\033[1;34m' _g=$'\033[1;32m' _r=$'\033[1;31m' _n=$'\033[0m'; else _b='' _g='' _r='' _n=''; fi
log()  { printf '%s[split-brain]%s %s\n' "$_b" "$_n" "$*" >&2; }
ok()   { printf '%s[ ok ]%s %s\n' "$_g" "$_n" "$*" >&2; }
fail() { printf '%s[fail]%s %s\n' "$_r" "$_n" "$*" >&2; }
die()  { fail "$*"; exit 1; }

emulator_healthy() {
  [[ -f "$EMULATOR_CA" ]] &&
    curl -fsS --noproxy '*' --max-time 2 --cacert "$EMULATOR_CA" "$EMULATOR_URL/__admin/health" >/dev/null 2>&1
}

ministack_healthy() {
  curl -fsS --noproxy '*' --max-time 2 "$MINISTACK_URL/_ministack/health" >/dev/null 2>&1
}

# wait_for <description> <function> [timeout seconds]
wait_for() {
  local what="$1" check="$2" timeout="${3:-60}" waited=0
  until "$check"; do
    (( waited >= timeout )) && return 1
    sleep 1; waited=$((waited + 1))
  done
  ok "$what healthy after ${waited}s"
}

port_in_use() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

stop_pidfile() {
  local name="$1" pidfile="$PID_DIR/$1.pid" pid
  [[ -f "$pidfile" ]] || return 0
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
    ok "stopped $name (pid $pid)"
  fi
  rm -f "$pidfile"
}

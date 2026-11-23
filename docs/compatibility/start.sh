#!/usr/bin/env bash
# Mark 50 probe launcher. Ports 9301 (AWS Emulator) and 4601 (MiniStack) only.
# Usage: start.sh [emu|ms|both]   (default both). Writes PIDs to <this folder>/pids/
set -u
P="$(cd "$(dirname "$0")" && pwd)"
mkdir -p $P/pids $P/logs
what=${1:-both}

clean_env() {
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN -u AWS_CA_BUNDLE \
      -u AWS_PROFILE -u AWS_DEFAULT_REGION -u AWS_REGION -u AWS_ENDPOINT_URL \
      NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 M50_CANARY=host-secret-canary "$@"
}

ms_ms() { date +%s%3N; }

if [[ $what == emu || $what == both ]]; then
  t0=$(ms_ms)
  clean_env EMULATOR_TELEMETRY=off aws-emulator -p 9301 -t off > $P/logs/emu.log 2>&1 &
  echo $! > $P/pids/emu.pid
  for i in $(seq 1 300); do
    if [[ -f ~/.aws/emulator/9301/ca.pem ]] && curl -s --noproxy '*' --cacert ~/.aws/emulator/9301/ca.pem \
         -o /dev/null -w '%{http_code}' https://127.0.0.1:9301/__admin/health 2>/dev/null | grep -q 200; then
      echo "emu_startup_ms=$(( $(ms_ms) - t0 ))"; break; fi
    sleep 0.05
  done
fi

if [[ $what == ms || $what == both ]]; then
  t0=$(ms_ms)
  clean_env GATEWAY_PORT=4601 BIND_HOST=127.0.0.1 IOT_MTLS_ENABLED=0 SFTP_ENABLED=0 \
      STATE_DIR=/tmp/mark50-ministack-state S3_DATA_DIR=/tmp/mark50-ministack-data/s3 \
      ${MS_EXTRA_ENV:-} ${MS_BIN:-ministack} > $P/logs/ms.log 2>&1 &
  echo $! > $P/pids/ms.pid
  for i in $(seq 1 600); do
    if curl -s --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:4601/_ministack/health 2>/dev/null | grep -q 200; then
      echo "ms_startup_ms=$(( $(ms_ms) - t0 ))"; break; fi
    sleep 0.05
  done
fi

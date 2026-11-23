#!/usr/bin/env bash
# Start AWS Emulator (HTTPS :9297) and MiniStack (HTTP :4566), wait for both.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

mkdir -p "$PID_DIR" "$LOG_DIR" "$LOCAL_DIR/ministack/s3" "$LOCAL_DIR/ministack/state"
python3 "$REPO_ROOT/tools/render_routes.py"

if [[ "$SPLIT_BRAIN_MODE" == "compose" ]]; then
  log "compose mode: docker compose up (not verified in CI, see README)"
  docker compose -f "$REPO_ROOT/docker-compose.yml" up -d --build
  wait_for "AWS Emulator" emulator_healthy 180 || die "AWS Emulator did not become healthy"
  wait_for "MiniStack" ministack_healthy 120 || die "MiniStack did not become healthy"
  exit 0
fi

command -v aws-emulator >/dev/null || die "aws-emulator not on PATH (Linux: install the .deb/.rpm, see README)"
command -v ministack >/dev/null    || die "ministack not on PATH (pip install ministack==1.5.44)"

# --- AWS Emulator -----------------------------------------------------------
if emulator_healthy; then
  log "AWS Emulator already answering on :$EMULATOR_PORT, reusing it"
else
  port_in_use "$EMULATOR_PORT" && die "port $EMULATOR_PORT is taken by something that is not a healthy AWS Emulator"
  # --auth off: a Lambda executed by MiniStack cannot present the emulator's
  #   rotating EMULATOR... keys (MiniStack injects its own credentials), so
  #   signature checks would break every cross-emulator call. Bound to
  #   127.0.0.1 only. Tradeoff: signing mistakes and IAM are not tested here.
  # -t off: opt out of usage telemetry (on by default).
  log "starting AWS Emulator on $EMULATOR_URL (auth off, telemetry off)"
  # env -i: like MiniStack below, don't hand the emulator your shell's real
  # credentials, tokens or proxy settings. It only needs PATH and HOME (it
  # writes its CA to ~/.aws/emulator/<port>/).
  env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 EMULATOR_TELEMETRY=off \
    nohup aws-emulator --bind 127.0.0.1 --port "$EMULATOR_PORT" \
      --auth off --telemetry off >"$LOG_DIR/aws-emulator.log" 2>&1 &
  echo $! >"$PID_DIR/aws-emulator.pid"
  wait_for "AWS Emulator" emulator_healthy 60 || { tail -20 "$LOG_DIR/aws-emulator.log"; die "AWS Emulator did not become healthy"; }
fi

# --- MiniStack ----------------------------------------------------------------
if ministack_healthy; then
  log "MiniStack already answering on :$MINISTACK_PORT, reusing it"
else
  port_in_use "$MINISTACK_PORT" && die "port $MINISTACK_PORT is taken by something that is not a healthy MiniStack"
  # env -i: MiniStack's local Lambda executor copies ITS OWN environment into
  # every function. Started from a normal shell, your functions would inherit
  # your real AWS_SECRET_ACCESS_KEY, proxy settings and CA bundle. Give it a
  # clean, explicit environment instead.
  log "starting MiniStack on $MINISTACK_URL (clean environment)"
  env -i \
    PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 \
    GATEWAY_PORT="$MINISTACK_PORT" BIND_HOST=127.0.0.1 \
    MINISTACK_REGION=us-east-1 \
    AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
    LAMBDA_EXECUTOR=local \
    IOT_MTLS_ENABLED=0 SFTP_ENABLED=0 \
    S3_DATA_DIR="$LOCAL_DIR/ministack/s3" STATE_DIR="$LOCAL_DIR/ministack/state" \
    nohup ministack >"$LOG_DIR/ministack.log" 2>&1 &
  echo $! >"$PID_DIR/ministack.pid"
  wait_for "MiniStack" ministack_healthy 60 || { tail -20 "$LOG_DIR/ministack.log"; die "MiniStack did not become healthy"; }
fi

ok "split local AWS is up. Load it into your shell with:  source scripts/env.sh"

#!/usr/bin/env bash
# End-to-end check of the deployed stack, across both emulators:
#   CSV -> S3 (MiniStack) -> Lambda (MiniStack) -> SSM + DynamoDB (AWS Emulator)
#                                               -> SQS alert queue (MiniStack)
# Each run uses its own site-id prefix, so it never collides with earlier runs.
set -uo pipefail
source "$(dirname "$0")/lib.sh"
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/env.sh" || exit 1

TIMEOUT="${VERIFY_TIMEOUT:-45}"
PASSED=0; FAILED=0
check() { # check <description> <command...>
  local desc="$1"; shift
  if "$@"; then ok "PASS  $desc"; PASSED=$((PASSED + 1)); else fail "FAIL  $desc"; FAILED=$((FAILED + 1)); fi
}
retry() { # retry <seconds> <command...>
  local deadline=$((SECONDS + $1)); shift
  until "$@"; do (( SECONDS >= deadline )) && return 1; sleep 1; done
}

tf_out() { terraform -chdir="$REPO_ROOT/terraform" output -raw "$1"; }
BUCKET="$(tf_out bucket)" || die "no terraform outputs: run make deploy first"
TABLE="$(tf_out table)"; QUEUE_URL="$(tf_out alert_queue_url)"; FUNCTION="$(tf_out function)"

RUN="r$(date +%s)$RANDOM"
WORK="$LOCAL_DIR/verify/$RUN"; mkdir -p "$WORK"
sed -E "2,\$s/^/${RUN}-/" "$REPO_ROOT/sample-data/readings-2026-11-20.csv" >"$WORK/readings.csv"
log "run id $RUN: $(($(wc -l <"$WORK/readings.csv") - 1)) readings, 3 meters, 2 sites"

# --- 1. the split is real: each resource exists in exactly one emulator -------
log "placement"
check "bucket $BUCKET lives in MiniStack" \
  bash -c "aws s3api head-bucket --bucket '$BUCKET' >/dev/null"
check "bucket is NOT in AWS Emulator" \
  bash -c "! aws s3api head-bucket --bucket '$BUCKET' --endpoint-url '$EMULATOR_URL' 2>/dev/null"
check "table $TABLE lives in AWS Emulator" \
  bash -c "[[ \$(aws dynamodb describe-table --table-name '$TABLE' --query Table.TableStatus --output text) == ACTIVE ]]"
check "table is NOT in MiniStack" \
  bash -c "! aws dynamodb describe-table --table-name '$TABLE' --endpoint-url '$MINISTACK_URL' >/dev/null 2>&1"
check "function $FUNCTION carries the routing env from routing.toml" \
  bash -c "[[ \$(aws lambda get-function-configuration --function-name '$FUNCTION' --query 'Environment.Variables.AWS_ENDPOINT_URL_DYNAMODB' --output text) == '$AWS_ENDPOINT_URL_DYNAMODB' ]]"

# --- 2. happy path -------------------------------------------------------------
log "upload s3://$BUCKET/incoming/$RUN/readings.csv"
aws s3 cp "$WORK/readings.csv" "s3://$BUCKET/incoming/$RUN/readings.csv" --only-show-errors

row_count() {
  aws dynamodb scan --table-name "$TABLE" --select COUNT \
    --filter-expression "begins_with(pk, :run)" \
    --expression-attribute-values "{\":run\":{\"S\":\"$RUN-\"}}" --query Count --output text
}
alerts_json() { # non-destructive peek via MiniStack's test hook
  curl -fsS --noproxy '*' "$MINISTACK_URL/_ministack/sqs/messages" --get --data-urlencode "QueueUrl=$QUEUE_URL" |
    jq -c --arg run "$RUN-" '[.. | objects | select(has("Body") or has("body")) | (.Body // .body) | fromjson? | select((.site_id // "") | startswith($run))]'
}
rows_are() { [[ "$(row_count)" == "$1" ]]; }
alerts_are() { [[ "$(alerts_json | jq length)" == "$1" ]]; }

check "13 interval rows written to DynamoDB (AWS Emulator) within ${TIMEOUT}s" retry "$TIMEOUT" rows_are 13
check "3 alerts on the SQS queue (MiniStack)" retry 10 alerts_are 3
alert_types_are() { [[ "$(alerts_json | jq -r '[.[].type] | sort | join(",")')" == "$1" ]]; }
check "alert types are GAP, RESET, SPIKE" alert_types_are "GAP,RESET,SPIKE"

digest_emailed() { # MiniStack test hook: read what SES "sent"
  curl -fsS --noproxy '*' "$MINISTACK_URL/_ministack/ses/messages" |
    jq -e --arg run "$RUN-" '[.messages[][] | select(.Subject | contains($run))] | length == 1' >/dev/null
}
check "one alert digest email sent via SES (MiniStack)" retry 10 digest_emailed

item_is() { # item_is <pk> <sk> <expected "kwh quality">
  local got
  got="$(aws dynamodb get-item --table-name "$TABLE" \
    --key "{\"pk\":{\"S\":\"$RUN-$1\"},\"sk\":{\"S\":\"$2\"}}" \
    --query '[Item.kwh.N, Item.quality.S]' --output text)"
  [[ "$got" == "$3" ]] || { log "  got '$got', expected '$3'"; return 1; }
}
check "counter rollover 99985.25 -> 2.75 stored as 17.49 kWh, quality ROLLOVER" \
  item_is "LAG-OJODU-01#INV-1" 2026-11-20T08:45:00Z $'17.49\tROLLOVER'
check "meter reset stored with no kWh, quality RESET" \
  item_is "LAG-OJODU-01#GRID-1" 2026-11-20T08:45:00Z $'None\tRESET'

# --- 3. idempotency: same data again must not create rows or alerts ------------
log "replay: upload the same readings under a new key"
aws s3 cp "$WORK/readings.csv" "s3://$BUCKET/incoming/$RUN/replay.csv" --only-show-errors
replay_logged() {
  aws logs filter-log-events --log-group-name "/aws/lambda/$FUNCTION" \
    --filter-pattern "\"$RUN/replay.csv\"" --query 'events[].message' --output text 2>/dev/null |
    grep -q '"duplicates": 13'
}
if retry "$TIMEOUT" replay_logged; then
  ok "PASS  replay processed: Lambda log shows 13 duplicates"; PASSED=$((PASSED + 1))
else
  fail "FAIL  replay processed: no Lambda log line with 13 duplicates"; FAILED=$((FAILED + 1))
fi
check "still 13 rows after replay (conditional writes rejected duplicates)" rows_are 13
check "still 3 alerts after replay (no duplicate alerts)" alerts_are 3
check "still one digest email after replay" digest_emailed

# --- 4. a malformed file becomes an alert, not a crash loop --------------------
printf 'site_id,meter_id,timestamp\n%s-BAD,M1,2026-11-20T08:00:00Z\n' "$RUN" >"$WORK/bad.csv"
aws s3 cp "$WORK/bad.csv" "s3://$BUCKET/incoming/$RUN/bad.csv" --only-show-errors
bad_file_alert() {
  curl -fsS --noproxy '*' "$MINISTACK_URL/_ministack/sqs/messages" --get --data-urlencode "QueueUrl=$QUEUE_URL" |
    jq -e --arg src "s3://$BUCKET/incoming/$RUN/bad.csv" '[.. | objects | (.Body // .body // empty) | fromjson? | select(.type == "BAD_FILE" and .source == $src)] | length == 1' >/dev/null
}
check "malformed CSV produces one BAD_FILE alert" retry "$TIMEOUT" bad_file_alert

echo
if (( FAILED == 0 )); then
  ok "VERIFY PASS ($PASSED checks, run $RUN)"
else
  fail "VERIFY FAIL ($FAILED failed, $PASSED passed, run $RUN)"; exit 1
fi

#!/usr/bin/env bash
# Return both emulators to an empty state between test runs.
#   MiniStack    : POST /_ministack/reset wipes every service in-process.
#   AWS Emulator : state is in memory only and there is no reset route, so
#                  the only reset is a restart.
# Terraform state describes resources that no longer exist after this, so it
# is removed too. Run `make deploy` again afterwards.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

ministack_healthy && curl -fsS --noproxy '*' -X POST "$MINISTACK_URL/_ministack/reset" >/dev/null && ok "MiniStack reset"
if [[ "$SPLIT_BRAIN_MODE" == "compose" ]]; then
  # MiniStack shares the emulator's network namespace, so restart both.
  docker compose -f "$REPO_ROOT/docker-compose.yml" restart aws-emulator ministack
else
  stop_pidfile aws-emulator
fi
"$(dirname "$0")/up.sh"
rm -f "$REPO_ROOT"/terraform/terraform.tfstate "$REPO_ROOT"/terraform/terraform.tfstate.backup
ok "both emulators are empty; terraform state cleared (run: make deploy)"

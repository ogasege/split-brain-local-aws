# Source me:  source scripts/env.sh
# Puts the current shell on the split local AWS: every AWS CLI v2 / SDK /
# Terraform call goes to MiniStack or AWS Emulator per routing.toml, and
# nothing can fall through to a real AWS account.
# shellcheck shell=bash

_sb_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

if [[ ! -f "$_sb_root/.local/routes.env" ]]; then
  python3 "$_sb_root/tools/render_routes.py" >/dev/null
fi

# 1. Drop anything that could point at a real account.
unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_SESSION_TOKEN AWS_SECURITY_TOKEN \
      AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE AWS_CONTAINER_CREDENTIALS_FULL_URI \
      AWS_CONTAINER_CREDENTIALS_RELATIVE_URI

# 2. Static credentials. Both emulators accept them: MiniStack accepts any
#    key, AWS Emulator runs with --auth off (see README, "Credentials").
#    A non-numeric key maps to MiniStack account 000000000000, which is also
#    AWS Emulator's only account, so ARNs match across both.
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1

# 3. Never read the user's real config or credentials files.
export AWS_CONFIG_FILE="$_sb_root/.local/aws-config"
export AWS_SHARED_CREDENTIALS_FILE=/dev/null
export AWS_EC2_METADATA_DISABLED=true
export AWS_DISABLE_HOST_PREFIX_INJECTION=true
export AWS_PAGER=""

# 4. Per-service endpoints + the emulator CA (generated from routing.toml).
# shellcheck disable=SC1091
source "$_sb_root/.local/routes.env"

# 5. Local calls must bypass any corporate/agent proxy.
case ",${NO_PROXY:-}," in *",127.0.0.1,"*) ;; *) export NO_PROXY="localhost,127.0.0.1${NO_PROXY:+,$NO_PROXY}";; esac
export no_proxy="$NO_PROXY"

# 6. AWS CLI v1 silently ignores AWS_ENDPOINT_URL_SQS (and RDS, EMR, ...)
#    and sends those calls to real AWS endpoints. Refuse to continue with it.
if command -v aws >/dev/null 2>&1; then
  case "$(aws --version 2>&1)" in
    aws-cli/1.*) echo "split-brain: AWS CLI v1 found on PATH. It ignores per-service endpoints for SQS; install AWS CLI v2." >&2
                 return 1 2>/dev/null || exit 1 ;;
  esac
fi
unset _sb_root

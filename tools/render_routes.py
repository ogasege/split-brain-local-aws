#!/usr/bin/env python3
"""Render routing.toml into every format the toolchain reads.

Outputs (all generated, all git-ignored):
  .local/routes.env        shell exports for AWS CLI v2 and SDKs
  .local/aws-config        shared config file using a `services` section
  terraform/provider.gen.tf
                           AWS provider with per-service endpoints, plus the
                           routing env map injected into Lambda functions

Stdlib only (tomllib needs Python 3.11+), so it runs before any pip install.
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# routing key -> (env var suffix, shared-config key, terraform endpoints key)
# Env var suffix = the botocore service id, upper-cased, spaces -> "_".
SERVICES = {
    "lambda": ("LAMBDA", "lambda", "lambda"),
    "s3": ("S3", "s3", "s3"),
    "sqs": ("SQS", "sqs", "sqs"),
    "sns": ("SNS", "sns", "sns"),
    "ses": ("SES", "ses", "ses"),
    "iam": ("IAM", "iam", "iam"),
    "logs": ("CLOUDWATCH_LOGS", "cloudwatch_logs", "logs"),
    "events": ("EVENTBRIDGE", "eventbridge", "events"),
    "dynamodb": ("DYNAMODB", "dynamodb", "dynamodb"),
    "ssm": ("SSM", "ssm", "ssm"),
    "sts": ("STS", "sts", "sts"),
    "kms": ("KMS", "kms", "kms"),
    "secretsmanager": ("SECRETS_MANAGER", "secrets_manager", "secretsmanager"),
}


def load(path: Path) -> dict:
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    emulators = cfg["emulators"]
    if cfg["default"] not in emulators:
        sys.exit(f"default emulator {cfg['default']!r} is not defined under [emulators]")
    for name, svc in cfg["services"].items():
        if name not in SERVICES:
            sys.exit(f"unknown service {name!r}: add it to SERVICES in {Path(__file__).name}")
        if svc["to"] not in emulators:
            sys.exit(f"service {name!r} routes to unknown emulator {svc['to']!r}")
    return cfg


def ca_bundle(cfg: dict) -> str:
    bundles = {os.path.expanduser(e["ca_bundle"]) for e in cfg["emulators"].values() if "ca_bundle" in e}
    if len(bundles) > 1:
        sys.exit("more than one HTTPS emulator: concatenate their CAs into one bundle first")
    return bundles.pop() if bundles else ""


def endpoint_map(cfg: dict) -> dict[str, str]:
    return {name: cfg["emulators"][svc["to"]]["endpoint"] for name, svc in sorted(cfg["services"].items())}


def lambda_env(cfg: dict) -> dict[str, str]:
    """Variables a function needs so its own SDK calls follow the same routes."""
    env = {"AWS_ENDPOINT_URL": cfg["emulators"][cfg["default"]]["endpoint"]}
    for name, url in endpoint_map(cfg).items():
        env[f"AWS_ENDPOINT_URL_{SERVICES[name][0]}"] = url
    if ca := ca_bundle(cfg):
        env["AWS_CA_BUNDLE"] = ca
    return env


def render_env(cfg: dict) -> str:
    lines = ["# Generated from routing.toml by tools/render_routes.py. Do not edit.", ""]
    for key, value in lambda_env(cfg).items():
        lines.append(f"export {key}={value}")
    return "\n".join(lines) + "\n"


def render_aws_config(cfg: dict) -> str:
    default_ep = cfg["emulators"][cfg["default"]]["endpoint"]
    out = [
        "# Generated from routing.toml by tools/render_routes.py. Do not edit.",
        "[default]",
        f"region = {cfg['region']}",
        f"endpoint_url = {default_ep}",
        "services = split-brain",
    ]
    if ca := ca_bundle(cfg):
        out.append(f"ca_bundle = {ca}")
    out += ["", "[services split-brain]"]
    for name, url in endpoint_map(cfg).items():
        out += [f"{SERVICES[name][1]} =", f"  endpoint_url = {url}"]
    return "\n".join(out) + "\n"


def render_terraform(cfg: dict) -> str:
    eps = endpoint_map(cfg)
    width = max(len(SERVICES[n][2]) for n in eps)
    ep_lines = "\n".join(f'    {SERVICES[n][2].ljust(width)} = "{u}"' for n, u in eps.items())
    ca = ca_bundle(cfg)
    env = lambda_env(cfg)
    kw = max(len(k) for k in env)
    env_lines = "\n".join(f'    {k.ljust(kw)} = "{v}"' for k, v in env.items())
    return f"""# Generated from routing.toml by tools/render_routes.py. Do not edit.
# One provider, two emulators: each service has its own endpoint.

provider "aws" {{
  region     = "{cfg['region']}"
  access_key = "test"
  secret_key = "test"

  # Local emulators: no real account, no metadata service, no STS lookups.
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  s3_use_path_style           = true

  # AWS Emulator serves HTTPS with its own CA. HTTP endpoints ignore it.
  custom_ca_bundle = "{ca}"

  endpoints {{
{ep_lines}
  }}
}}

locals {{
  # Injected into every Lambda so the function's SDK calls follow routing.toml.
  routing_env = {{
{env_lines}
  }}
}}
"""


def main() -> None:
    cfg = load(ROOT / "routing.toml")
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    (local / "routes.env").write_text(render_env(cfg))
    (local / "aws-config").write_text(render_aws_config(cfg))
    (ROOT / "terraform" / "provider.gen.tf").write_text(render_terraform(cfg))
    if "--table" in sys.argv:
        print("| Service | Emulator | Why |\n| --- | --- | --- |")
        for name, svc in cfg["services"].items():
            print(f"| {name} | {svc['to']} | {svc['why']} |")
    else:
        print(f"rendered {len(cfg['services'])} routes -> .local/routes.env, .local/aws-config, terraform/provider.gen.tf")


if __name__ == "__main__":
    main()

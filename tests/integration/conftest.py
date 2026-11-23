import json
import os
import subprocess
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

import boto3
import pytest

ROOT = Path(__file__).resolve().parents[2]
ROUTING = tomllib.loads((ROOT / "routing.toml").read_text())
MINISTACK = ROUTING["emulators"]["ministack"]["endpoint"]
EMULATOR = ROUTING["emulators"]["aws-emulator"]["endpoint"]


def _up(url, path, ca=None):
    import ssl

    ctx = ssl.create_default_context(cafile=ca) if ca else None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    try:
        return opener.open(url + path, timeout=3).status == 200
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def stack():
    # `make integration` sets SPLIT_BRAIN_REQUIRE_STACK=1 so a missing stack
    # fails the run instead of silently skipping every test.
    _missing = pytest.fail if os.environ.get("SPLIT_BRAIN_REQUIRE_STACK") == "1" else pytest.skip
    if "AWS_ENDPOINT_URL_S3" not in os.environ:
        _missing("routing env not loaded: run via `make integration` or `source scripts/env.sh`")
    ca = os.environ.get("AWS_CA_BUNDLE")
    if not (_up(MINISTACK, "/_ministack/health") and _up(EMULATOR, "/__admin/health", ca)):
        _missing("emulators not running: make up")


@pytest.fixture(scope="session")
def outputs():
    raw = subprocess.run(
        ["terraform", f"-chdir={ROOT / 'terraform'}", "output", "-json"], check=True, capture_output=True, text=True
    ).stdout
    out = {k: v["value"] for k, v in json.loads(raw).items()}
    if not out:
        (pytest.fail if os.environ.get("SPLIT_BRAIN_REQUIRE_STACK") == "1" else pytest.skip)("nothing deployed: make deploy")
    return out


@pytest.fixture(scope="session")
def aws():
    """boto3 clients that resolve endpoints from AWS_ENDPOINT_URL_* only."""
    return lambda service: boto3.client(service)


def ministack_sqs_peek(queue_url):
    """Non-destructive read of a MiniStack queue via its test hook."""
    url = f"{MINISTACK}/_ministack/sqs/messages?" + urllib.parse.urlencode({"QueueUrl": queue_url})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.loads(opener.open(url, timeout=5).read())
    bodies = []

    def walk(node):
        if isinstance(node, dict):
            body = node.get("Body", node.get("body"))
            if isinstance(body, str):
                bodies.append(body)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    out = []
    for b in bodies:
        try:
            out.append(json.loads(b))
        except ValueError:
            pass
    return out


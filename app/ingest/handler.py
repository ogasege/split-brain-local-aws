"""S3 -> Lambda entry point for meter-reading CSV files.

Runs unchanged on real AWS and on the split local setup. The handler never
knows which emulator a service lives in: boto3 resolves every endpoint from
AWS_ENDPOINT_URL_<SERVICE> variables that the deployment injects.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from ingest.meter_math import BadInput, Interval, MeterConfig, compute_intervals, iso, parse_csv

log = logging.getLogger()
if not log.handlers:
    # Real Lambda installs a handler on the root logger. MiniStack's local
    # executor (plain CPython) does not, so INFO lines would be dropped.
    logging.basicConfig(format="%(levelname)s %(message)s")
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ["TABLE_NAME"]
ALERT_QUEUE_URL = os.environ["ALERT_QUEUE_URL"]
CONFIG_PARAM = os.environ.get("CONFIG_PARAM", "/split-brain/telemetry/config")
# Optional human-readable digest for the site operations team.
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO")

# Clients are created once per execution environment, as on real Lambda.
s3 = boto3.client("s3")
ddb = boto3.client("dynamodb")
ssm = boto3.client("ssm")
sqs = boto3.client("sqs")
ses = boto3.client("ses")

_config_cache: MeterConfig | None = None


def load_config() -> MeterConfig:
    """Thresholds come from Parameter Store.

    No silent fallback to defaults: a missing parameter usually means the
    function is talking to the wrong endpoint, and defaults would hide that.
    """
    global _config_cache
    if _config_cache is None:
        raw = ssm.get_parameter(Name=CONFIG_PARAM)["Parameter"]["Value"]
        _config_cache = MeterConfig.from_dict(json.loads(raw))
    return _config_cache


def to_item(iv: Interval, source: str) -> dict:
    item = {
        "pk": {"S": f"{iv.site_id}#{iv.meter_id}"},
        "sk": {"S": iso(iv.end)},
        "site_id": {"S": iv.site_id},
        "interval_start": {"S": iso(iv.start)},
        "quality": {"S": iv.quality},
        "source": {"S": source},
    }
    if iv.kwh is not None:
        item["kwh"] = {"N": str(iv.kwh.quantize(Decimal("0.001")))}
    return item


def write_interval(iv: Interval, source: str) -> bool:
    """Idempotent write. Returns False if this interval was already stored.

    S3 delivers events at least once and people re-upload files, so the same
    interval must never be counted twice. The condition makes the write a
    no-op for an existing (pk, sk) without a read-before-write race.
    """
    try:
        ddb.put_item(
            TableName=TABLE_NAME,
            Item=to_item(iv, source),
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def send_alerts(alerts: list[dict]) -> None:
    # SQS batches max out at 10 entries.
    for i in range(0, len(alerts), 10):
        chunk = alerts[i : i + 10]
        resp = sqs.send_message_batch(
            QueueUrl=ALERT_QUEUE_URL,
            Entries=[{"Id": str(n), "MessageBody": json.dumps(a)} for n, a in enumerate(chunk)],
        )
        if resp.get("Failed"):
            raise RuntimeError(f"alert batch partially failed: {resp['Failed']}")


def email_digest(source: str, alerts: list[dict]) -> None:
    """One email per file, not per alert: operators read these on a phone."""
    if not (ALERT_EMAIL_FROM and ALERT_EMAIL_TO):
        return
    lines = [
        f"{a['type']:<6} {a['site_id']} / {a['meter_id']} at {a['interval_end']} (kWh: {a['kwh'] or 'unknown'})"
        for a in alerts
    ]
    sites = sorted({a["site_id"] for a in alerts})
    ses.send_email(
        Source=ALERT_EMAIL_FROM,
        Destination={"ToAddresses": [ALERT_EMAIL_TO]},
        Message={
            "Subject": {"Data": f"[meter-alerts] {len(alerts)} alert(s) for {', '.join(sites)}"},
            "Body": {"Text": {"Data": f"Source: {source}\n\n" + "\n".join(lines)}},
        },
    )


def process_object(bucket: str, key: str) -> dict:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    source = f"s3://{bucket}/{key}"
    try:
        readings = parse_csv(body)
    except BadInput as exc:
        alert = {"type": "BAD_FILE", "source": source, "detail": str(exc)}
        send_alerts([alert])
        return {"source": source, "rejected": str(exc)}

    intervals = compute_intervals(readings, load_config())
    written = duplicates = 0
    alerts = []
    for iv in intervals:
        if write_interval(iv, source):
            written += 1
            if iv.needs_alert:
                alerts.append(
                    {
                        "type": iv.quality,
                        "site_id": iv.site_id,
                        "meter_id": iv.meter_id,
                        "interval_end": iso(iv.end),
                        "kwh": None if iv.kwh is None else str(iv.kwh),
                        "source": source,
                    }
                )
        else:
            duplicates += 1  # replayed event or re-uploaded file: no new alert
    if alerts:
        send_alerts(alerts)
        email_digest(source, alerts)
    summary = {
        "source": source,
        "readings": len(readings),
        "intervals": len(intervals),
        "written": written,
        "duplicates": duplicates,
        "alerts": len(alerts),
    }
    log.info(json.dumps(summary))
    return summary


def handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # S3 event keys are URL-encoded ("+" for spaces).
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        results.append(process_object(bucket, key))
    return {"results": results}

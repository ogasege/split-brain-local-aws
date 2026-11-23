"""Handler tests with in-memory fakes: no emulator, no network."""
import importlib
import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

SAMPLE = (Path(__file__).resolve().parents[2] / "sample-data" / "readings-2026-11-20.csv").read_text()


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)].encode())}


class FakeDynamoDB:
    """Enforces the same attribute_not_exists condition as DynamoDB."""

    def __init__(self):
        self.items = {}

    def put_item(self, TableName, Item, ConditionExpression):
        assert "attribute_not_exists" in ConditionExpression
        key = (Item["pk"]["S"], Item["sk"]["S"])
        if key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem")
        self.items[key] = Item


class FakeSSM:
    def __init__(self, value=None):
        self.value = value

    def get_parameter(self, Name):
        if self.value is None:
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": Name}}, "GetParameter")
        return {"Parameter": {"Value": self.value}}


class FakeSQS:
    def __init__(self):
        self.sent = []

    def send_message_batch(self, QueueUrl, Entries):
        assert len(Entries) <= 10
        self.sent += [json.loads(e["MessageBody"]) for e in Entries]
        return {"Successful": Entries}


class FakeSES:
    def __init__(self):
        self.sent = []

    def send_email(self, Source, Destination, Message):
        self.sent.append({"from": Source, "to": Destination["ToAddresses"], "subject": Message["Subject"]["Data"],
                          "body": Message["Body"]["Text"]["Data"]})
        return {"MessageId": "m"}


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "t")
    monkeypatch.setenv("ALERT_QUEUE_URL", "http://q")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "alerts@example.com")
    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@example.com")
    import ingest.handler as mod

    mod = importlib.reload(mod)
    mod.s3 = FakeS3({("b", "incoming/a b.csv"): SAMPLE, ("b", "incoming/bad.csv"): "x,y\n1,2\n"})
    mod.ddb = FakeDynamoDB()
    mod.ssm = FakeSSM(json.dumps({"max_interval_kwh": 50}))
    mod.sqs = FakeSQS()
    mod.ses = FakeSES()
    mod._config_cache = None
    return mod


def event(key):
    return {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": key}}}]}


def test_happy_path_writes_rows_and_alerts(h):
    out = h.handler(event("incoming/a+b.csv"), None)["results"][0]  # "+" is an encoded space
    assert (out["written"], out["duplicates"], out["alerts"]) == (13, 0, 3)
    assert len(h.ddb.items) == 13
    assert sorted(a["type"] for a in h.sqs.sent) == ["GAP", "RESET", "SPIKE"]
    assert len(h.ses.sent) == 1  # one digest per file, not one email per alert
    assert h.ses.sent[0]["subject"] == "[meter-alerts] 3 alert(s) for ACC-OSU-02, LAG-OJODU-01"


def test_replay_is_idempotent(h):
    h.handler(event("incoming/a+b.csv"), None)
    out = h.handler(event("incoming/a+b.csv"), None)["results"][0]
    assert (out["written"], out["duplicates"], out["alerts"]) == (0, 13, 0)
    assert len(h.sqs.sent) == 3
    assert len(h.ses.sent) == 1


def test_reset_interval_has_no_kwh_attribute(h):
    h.handler(event("incoming/a+b.csv"), None)
    item = h.ddb.items[("LAG-OJODU-01#GRID-1", "2026-11-20T08:45:00Z")]
    assert item["quality"]["S"] == "RESET" and "kwh" not in item


def test_bad_file_becomes_an_alert(h):
    out = h.handler(event("incoming/bad.csv"), None)["results"][0]
    assert "missing columns" in out["rejected"]
    assert h.sqs.sent[0]["type"] == "BAD_FILE"


def test_missing_config_fails_fast(h):
    # A missing parameter usually means the function is pointed at the wrong
    # emulator. Failing beats silently running on defaults.
    h.ssm = FakeSSM(None)
    with pytest.raises(ClientError, match="ParameterNotFound"):
        h.handler(event("incoming/a+b.csv"), None)


def test_alerts_are_batched_by_ten(h):
    h.send_alerts([{"n": i} for i in range(23)])
    assert len(h.sqs.sent) == 23


def test_no_email_when_digest_not_configured(h):
    h.ALERT_EMAIL_TO = None
    h.handler(event("incoming/a+b.csv"), None)
    assert h.ses.sent == [] and len(h.sqs.sent) == 3

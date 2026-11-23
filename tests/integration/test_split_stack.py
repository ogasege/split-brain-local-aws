"""Integration tests against the running split stack (make up deploy)."""
import time
import uuid
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from conftest import EMULATOR, MINISTACK, ROUTING, ministack_sqs_peek

SAMPLE = (Path(__file__).resolve().parents[2] / "sample-data" / "readings-2026-11-20.csv").read_text()


def wait_until(fn, timeout=45, interval=1.0):
    deadline = time.time() + timeout
    while True:
        result = fn()
        if result or time.time() > deadline:
            return result
        time.sleep(interval)


@pytest.mark.parametrize("service", sorted(ROUTING["services"]))
def test_sdk_resolves_every_route_from_routing_toml(aws, service):
    expected = ROUTING["emulators"][ROUTING["services"][service]["to"]]["endpoint"]
    assert aws(service).meta.endpoint_url == expected


def test_unrouted_service_falls_back_to_an_emulator_not_real_aws(aws):
    # sns is not in routing.toml: it must land on the default emulator.
    assert aws("sns").meta.endpoint_url == EMULATOR


def test_dynamodb_conditional_write_semantics_on_aws_emulator(aws, outputs):
    ddb = aws("dynamodb")
    key = {"pk": {"S": f"it-{uuid.uuid4()}"}, "sk": {"S": "x"}}
    ddb.put_item(TableName=outputs["table"], Item=key, ConditionExpression="attribute_not_exists(pk)")
    with pytest.raises(ClientError) as err:
        ddb.put_item(
            TableName=outputs["table"],
            Item=key,
            ConditionExpression="attribute_not_exists(pk)",
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    assert err.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    assert err.value.response["Item"] == key


def test_end_to_end_upload_produces_rows_and_alerts(aws, outputs):
    run = f"it{uuid.uuid4().hex[:8]}"
    lines = SAMPLE.strip().splitlines()
    body = "\n".join([lines[0]] + [f"{run}-{line}" for line in lines[1:]]) + "\n"
    aws("s3").put_object(Bucket=outputs["bucket"], Key=f"incoming/{run}/readings.csv", Body=body.encode())

    def count():
        resp = aws("dynamodb").scan(
            TableName=outputs["table"],
            Select="COUNT",
            FilterExpression="begins_with(pk, :p)",
            ExpressionAttributeValues={":p": {"S": f"{run}-"}},
        )
        return resp["Count"] == 13

    assert wait_until(count), "Lambda did not write 13 rows to the AWS Emulator table"
    def run_alerts():
        found = [a for a in ministack_sqs_peek(outputs["alert_queue_url"]) if a.get("site_id", "").startswith(run)]
        return found if len(found) == 3 else None

    alerts = wait_until(run_alerts, timeout=10)
    assert alerts and sorted(a["type"] for a in alerts) == ["GAP", "RESET", "SPIKE"]


def test_cross_emulator_s3_notification_is_refused(aws, outputs):
    """Guard rail: an event source in one emulator cannot target the other.

    AWS Emulator validates the destination against its own Lambda service,
    as real S3 does, and refuses a function that only MiniStack hosts.
    """
    import boto3

    s3_emulator = boto3.client("s3", endpoint_url=EMULATOR)
    bucket = f"cross-{uuid.uuid4().hex[:10]}"
    s3_emulator.create_bucket(Bucket=bucket)
    try:
        fn_arn = aws("lambda").get_function(FunctionName=outputs["function"])["Configuration"]["FunctionArn"]
        with pytest.raises(ClientError) as err:
            s3_emulator.put_bucket_notification_configuration(
                Bucket=bucket,
                NotificationConfiguration={
                    "LambdaFunctionConfigurations": [{"LambdaFunctionArn": fn_arn, "Events": ["s3:ObjectCreated:*"]}]
                },
            )
        assert err.value.response["Error"]["Code"] == "InvalidArgument"
    finally:
        s3_emulator.delete_bucket(Bucket=bucket)


def test_function_environment_has_routing_but_no_static_secrets(aws, outputs):
    env = aws("lambda").get_function_configuration(FunctionName=outputs["function"])["Environment"]["Variables"]
    assert env["AWS_ENDPOINT_URL_DYNAMODB"] == EMULATOR
    assert env["AWS_ENDPOINT_URL_S3"] == MINISTACK
    assert env["AWS_CA_BUNDLE"].endswith("ca.pem")
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} & set(env)

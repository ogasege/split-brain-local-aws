# AWS Emulator 1.0.589 vs MiniStack 1.5.44: probe matrix (Mark 50)

Status: FINAL (2026-11-23). Every claim below was measured in this workspace unless it cites a URL.
Harness: `probe.py` (this folder) runs the same boto3 calls against both targets (per-check timeout, AWS_* env stripped, explicit endpoints, no real AWS).
Raw data: `results.json` (default configs + `ergonomics` + `cross`), `results_ms_auth.json` (MiniStack with `AUTH=true`), `results_ms_duckdb.json` (MiniStack with DuckDB importable), `fidelity_scores_v1.0.322.txt` (published AWS Emulator scores, aggregated).
Helpers: `start.sh` / `stop.sh` (ports 9301 / 4601 only), `ergonomics.py`, `gen_matrix.py` (renders the table below from results.json).
Legend: PASS = matched AWS for what we checked. WRONG = call succeeded but result differs from AWS. FAIL = unexpected error. 501 = not implemented (none hit).

## Headline findings
1. **AWS Emulator enforces IAM identity policies on assumed-role credentials** (the brief said it does not evaluate IAM; the docs say it does not evaluate IAM/resource policies "the way AWS does" and not to use it for security testing). Measured: a Lambda whose execution role has no policies gets `AccessDenied ... because no identity-based policy allows the s3:ListAllMyBuckets action`; an assumed role with inline `Deny *` gets `... with an explicit deny in an identity-based policy`. The root `EMULATOR...` keys bypass it, and it still applies with `--auth off`. Also: a role without `logs:*` permissions produces no CloudWatch log lines (same as AWS).
2. **AWS Emulator validates resource policies on wiring like AWS does**: S3 notification to SQS without a queue policy, or to Lambda without `lambda:AddPermission`, is rejected with `InvalidArgument: Unable to validate the following destination configurations`; SNS->SQS and EventBridge->SQS deliver **0** messages without a queue policy for the service principal; Secrets Manager rotation needs the function policy; Scheduler needs a role trusting `scheduler.amazonaws.com`; SQS event source mapping needs `sqs:ReceiveMessage` on the role; API Gateway returns `500 {"message":"Internal Server Error"}` until the Lambda permission exists. Default MiniStack accepts all of these.
3. **MiniStack has an `AUTH=true` switch that closes most of that gap** (README mentions it only for permission boundaries). With it: IAM deny and missing-permission are enforced inside Lambda, presigned signatures are checked, S3->SQS without policy is rejected, SNS->SQS without policy delivers 0. Still not enforced with `AUTH=true`: EventBridge->SQS without queue policy (delivered 1), S3->Lambda without permission (accepted), Secrets Manager rotation without permission (accepted).
4. **AWS Emulator runs Lambda in-process on embedded interpreters**: Python reports `3.12.0 (...) [RustPython 0.6.0 with rustc 1.99.1 ...]`; Node reports `v20.0.0`. No host filesystem (`OSError: [Errno 38] a Lambda function in this emulator has no host filesystem to reach`), no network except AWS service hosts, which are routed back into the emulator (`gaierror('https://example.com/ addresses no service this emulator serves: sqs, sns, sts, ...')`). A bundled boto3 (`/var/runtime/boto3`, reports `1.0.0`) works in-function with the role's credentials. No C extensions (`No module named '_sqlite3'`, `No module named '_ctypes'`), so only pure-Python deps import.
5. **MiniStack runs Lambda as host subprocesses**: asked for `python3.12`, ran `3.13.18`; asked for `nodejs20.x`, ran `v22.23.5`. The whole MiniStack process environment leaks into the function (262 vars, including our canary `M50_CANARY=host-secret-canary`). Host site-packages import without packaging (`requests`, `numpy`), so a missing dependency is not caught. Functions reach the internet and the host filesystem. It injects `AWS_ENDPOINT_URL=http://127.0.0.1:4601`, so in-function boto3 just works. One warm `_worker.py` per function stays alive (15 workers, 573 MB RSS after the run).
6. **Athena**: AWS Emulator executed real SQL over a CSV in S3 (`CREATE EXTERNAL TABLE` + `GROUP BY`, correct sums). MiniStack from pip (no DuckDB) **returns SUCCEEDED with fake rows** `[['result'], ['mock_value']]`. With DuckDB importable (our venv, equivalent to the `:full` image claim) it returned correct rows, but needs a Glue database first: the implicit `default` database fails with `Schema 'default' does not exist`.
7. **Ergonomics favour MiniStack**: `POST /_ministack/reset` wipes state in 8 ms; AWS Emulator has no reset route (`/__admin/reset` 404), so reset = restart (~0.3 s) and **credentials rotate on every restart** (old key -> `InvalidAccessKeyId`). Idle RSS 41 MB vs 323-328 MB; after the full run 117 MB (+573 MB Lambda workers) vs 860 MB. Binary: AWS Emulator is a single 1.2 GB executable.

## Setup facts (measured)
- AWS Emulator: `EMULATOR_TELEMETRY=off aws-emulator -p 9301 -t off`. HTTPS, binds 127.0.0.1. Writes `~/.aws/emulator/9301/{ca.pem,config,emulator_credentials,truststore.p12}` (no `emulator_credentials` with `-a off`). Health 200 in 303-416 ms. The generated `config` sets `s3 = signature_version = s3v4`; lose that and presigned URLs break (see Surprises).
- MiniStack: `GATEWAY_PORT=4601 ministack`. Binds **0.0.0.0** by default (`BIND_HOST=127.0.0.1` to keep it local). Health 200 in 217-396 ms. No CLI port flag (`-d/--detach`, `--stop` only).
- Running two MiniStacks on one machine: IoT mTLS MQTT listener binds **8883** at boot (`IOT_MTLS_PORT=<n>` moves it, `IOT_MTLS_ENABLED=0` disables); SFTP only binds on CreateServer/restored state (`SFTP_ENABLED=0`); default dirs are shared (`STATE_DIR=/tmp/ministack-state`, `S3_DATA_DIR=/tmp/ministack-data/s3`), give each instance its own.
- Probe launch line: `GATEWAY_PORT=4601 BIND_HOST=127.0.0.1 IOT_MTLS_ENABLED=0 SFTP_ENABLED=0 STATE_DIR=/tmp/mark50-ministack-state S3_DATA_DIR=/tmp/mark50-ministack-data/s3 ministack`
- **S3 addressing (confirmed): AWS Emulator needs path-style in practice.** Its TLS cert SAN is only `DNS:localhost, IP:127.0.0.1, IP:::1` (subject `CN = rcgen self signed cert`), so a virtual-hosted request fails before it reaches S3: `SSLError("SSL validation failed for https://<bucket>.localhost:9301/k hostname '<bucket>.localhost' doesn't match either of 'localhost', '127.0.0.1', '0:0:0:0:0:0:0:1'")`. With endpoint `https://127.0.0.1:9301` botocore uses path-style automatically even when `addressing_style=virtual` (IP host), so the default setup works; with `https://localhost:9301`, `auto` and `path` work and `virtual` breaks. Set `s3 = addressing_style = path` (or Terraform `s3_use_path_style = true`, SDK `forcePathStyle: true`) explicitly so it does not depend on the hostname. MiniStack serves both styles: virtual-hosted `<bucket>.localhost:4601` worked once `*.localhost` resolved to 127.0.0.1 (it does not resolve in this container: `EndpointConnectionError ... http://<bucket>.localhost:4601/k`). Evidence: `results.json["s3_addressing"]`.
- Account identity: AWS Emulator `arn:aws:iam::000000000000:user/emulator`; MiniStack `arn:aws:iam::000000000000:root`. Both issue identical ARNs for same-named resources (`arn:aws:sqs:us-east-1:000000000000:<name>`).

## Check matrix (default configs: AWS Emulator auth on, MiniStack AUTH unset)
<!-- TABLE START -->
| Check | AWS Emulator 1.0.589 | MiniStack 1.5.44 |
|---|---|---|
| `s3.basic_put_get` | **PASS** {"etag": "\"5d41402abc4b2a76b9719d911017c592\"", "ctype": "text/plain", "missing_key_error": {"http": 404, "code": "NoSuchKey", "message": "The specified key does not exist."}} | **PASS** {"etag": "\"5d41402abc4b2a76b9719d911017c592\"", "ctype": "text/plain", "missing_key_error": {"http": 404, "code": "NoSuchKey", "message": "The specified key does not exist."}} |
| `s3.multipart` | **PASS** {"etag": "\"30dcfd3901d1c613b7fb532281748544-2\"", "size": 5242884, "small_part_behaviour": {"http": 400, "code": "EntityTooSmall", "message": "Your proposed upload is smaller than the minimum allowed size"}} | **WRONG** accepted 1 KiB non-final part (AWS: EntityTooSmall) / {'etag': '"30dcfd3901d1c613b7fb532281748544-2"', 'size': 5242884, 'small_part_behaviour': 'accepted 1 KiB non-final part (AWS: EntityTooSmall)'} |
| `s3.presigned_url` | **PASS** {"url_host": "127.0.0.1:9301", "tampered_status": 403, "tampered_body": "<Error><Code>SignatureDoesNotMatch</Code><Message>The request signature we calculated does not match the signature you provided. Check your key and signing m... | **WRONG** tampered signature accepted / {'url_host': '127.0.0.1:4601', 'tampered_status': 200, 'tampered_body': 'presigned-body', 'expired_status': 403, 'expired_body': '<?xml version="1.0" encoding="UTF-8"?>
<Error><Code>AccessDenied</Cod... |
| `s3.versioning` | **PASS** {"versions": 2, "delete_markers": 1, "delete_marker_flag": true, "status": "Enabled", "get_after_delete": {"http": 404, "code": "NoSuchKey", "message": "The specified key does not exist."}} | **PASS** {"versions": 2, "delete_markers": 1, "delete_marker_flag": true, "status": "Enabled", "get_after_delete": {"http": 404, "code": "NoSuchKey", "message": "The specified key does not exist."}} |
| `s3.notify_sqs` | **PASS** {"messages": 2, "s3_TestEvent_seen": true, "config_without_queue_policy": {"http": 400, "code": "InvalidArgument", "message": "Unable to validate the following destination configurations"}, "event_names": ["ObjectCreated:Put"], "k... | **PASS** {"messages": 3, "s3_TestEvent_seen": true, "config_without_queue_policy": "ACCEPTED without queue policy (AWS: InvalidArgument Unable to validate destination)", "event_names": ["ObjectCreated:Put"], "key": ["up/file.txt"]} |
| `s3.notify_lambda` | **PASS** {"fired": {"via": "signal_queue (boto3 inside the function)", "count": 1, "event_sample": "{\"fn\": \"m50-s3fn-a44109-641e\", \"event\": {\"Records\": [{\"eventVersion\": \"2.1\", \"eventSource\": \"aws:s3\", \"awsRegion\": \"us-e... | **PASS** {"fired": {"via": "signal_queue (boto3 inside the function)", "count": 1, "event_sample": "{\"fn\": \"m50-s3fn-a44109-5318\", \"event\": {\"Records\": [{\"eventVersion\": \"2.6\", \"eventSource\": \"aws:s3\", \"awsRegion\": \"us-e... |
| `ddb.conditional_put_fails` | **PASS** {"http": 400, "code": "ConditionalCheckFailedException", "message": "The conditional request failed", "ALL_OLD_item_returned": true} | **PASS** {"http": 400, "code": "ConditionalCheckFailedException", "message": "The conditional request failed", "ALL_OLD_item_returned": true} |
| `ddb.transact_cancel_reasons` | **PASS** {"http": 400, "code": "TransactionCanceledException", "message": "Transaction cancelled, please refer cancellation reasons for specific reasons [None, ConditionalCheckFailed]", "CancellationReasons": [{"Code": "None"}, {"Code": "C... | **PASS** {"http": 400, "code": "TransactionCanceledException", "message": "Transaction cancelled, please refer cancellation reasons for specific reasons [None, ConditionalCheckFailed]", "CancellationReasons": [{"Code": "None"}, {"Code": "C... |
| `ddb.gsi_query` | **PASS** {"count": 2, "consistent_read_on_gsi": {"http": 400, "code": "ValidationException", "message": "Consistent reads are not supported on global secondary indexes"}} | **PASS** {"count": 2, "consistent_read_on_gsi": {"http": 400, "code": "ValidationException", "message": "Consistent reads are not supported on global secondary indexes"}} |
| `ddb.streams` | **PASS** {"records": 2, "events": ["INSERT", "MODIFY"], "has_old_image_on_modify": true} | **PASS** {"records": 2, "events": ["INSERT", "MODIFY"], "has_old_image_on_modify": true} |
| `ddb.ttl` | **PASS** {"describe": {"TimeToLiveStatus": "ENABLING", "AttributeName": "exp"}, "expired_item_still_readable_after_3s": false, "note": "AWS deletes expired items lazily (up to days), so 'still readable' is AWS-consistent"} | **PASS** {"describe": {"TimeToLiveStatus": "ENABLED", "AttributeName": "exp"}, "expired_item_still_readable_after_3s": true, "note": "AWS deletes expired items lazily (up to days), so 'still readable' is AWS-consistent"} |
| `ddb.pagination` | **PASS** {"pages": 3, "total": 25} | **PASS** {"pages": 3, "total": 25} |
| `ddb.error_texts` | **PASS** {"missing_table": {"http": 400, "code": "ResourceNotFoundException", "message": "Requested resource not found"}, "bad_key_type": {"http": 400, "code": "ValidationException", "message": "The provided key element does not match the ... | **WRONG** accepted invalid input: ['bad_expression'] / {'missing_table': {'http': 400, 'code': 'ResourceNotFoundException', 'message': 'Requested resource not found'}, 'bad_key_type': {'http': 400, 'code': 'ValidationException', 'message': ... |
| `sqs.standard` | **PASS** {"md5_body": "49f68a5c8493ec2c0bf489821c21fc3b", "attrs": ["SentTimestamp", "ApproximateReceiveCount", "ApproximateFirstReceiveTimestamp", "SenderId"]} | **PASS** {"md5_body": "49f68a5c8493ec2c0bf489821c21fc3b", "attrs": ["SenderId", "SentTimestamp", "ApproximateReceiveCount", "ApproximateFirstReceiveTimestamp"]} |
| `sqs.fifo_dedup` | **PASS** {"bodies": ["dup", "second"], "no_dedup_id_error": {"http": 400, "code": "InvalidParameterValue", "message": "The queue should either have ContentBasedDeduplication enabled or MessageDeduplicationId provided explicitly"}} | **PASS** {"bodies": ["dup", "second"], "no_dedup_id_error": {"http": 400, "code": "InvalidParameterValue", "message": "The queue should either have ContentBasedDeduplication enabled or MessageDeduplicationId provided explicitly."}} |
| `sqs.dlq_redrive` | **PASS** {"receives_from_source": 2, "in_dlq": 1, "start_message_move_task": "ok"} | **PASS** {"receives_from_source": 2, "in_dlq": 1, "start_message_move_task": "ok"} |
| `sqs.long_poll` | **PASS** {"empty_wait_s": 3.0, "woke_after_s": 1.03, "got": 1} | **PASS** {"empty_wait_s": 3.0, "woke_after_s": 1.01, "got": 1} |
| `sns.sqs_filter_policy` | **PASS** {"delivered_without_queue_policy": 0, "filtered_queue": ["m-red"], "unfiltered_queue_count": 2, "unfiltered_envelope_type": "Notification"} | **PASS** {"delivered_without_queue_policy": 1, "filtered_queue": ["m-red"], "unfiltered_queue_count": 2, "unfiltered_envelope_type": "Notification"} |
| `lambda.invoke_env` | **PASS** {"status": 200, "payload": {"ok": true, "my_var": "hello-emu", "fn": "m50-inv-a44109-2231", "python": "3.12.0", "marker": "/tmp/mark50-markers/a44109/emu/invoke/m50-inv-a44109-2231-1795465936071794797.json", "aws_region_env": "us-... | **PASS** {"status": 200, "payload": {"ok": true, "my_var": "hello-ms", "fn": "m50-inv-a44109-587d", "python": "3.13.18", "marker": "/tmp/mark50-markers/a44109/ms/invoke/m50-inv-a44109-587d-1795465911007907046.json", "aws_region_env": "us-e... |
| `lambda.error_shape` | **PASS** {"status": 200, "FunctionError": "Unhandled", "payload": {"errorMessage": "boom requested by probe", "errorType": "ValueError", "requestId": "e4bbbb01-ae86-4245-bba1-eb0a558923a5", "stackTrace": ["  File \"/var/task/app.py\", line... | **PASS** {"status": 200, "FunctionError": "Unhandled", "payload": {"errorMessage": "boom requested by probe", "errorType": "ValueError", "requestId": "a7ea7fd3-4dc0-4509-ac4c-ca0352acdd6b", "stackTrace": ["  File \"/tmp/ministack-lambda-m5... |
| `lambda.esm_sqs` | **PASS** {"fired": {"via": "signal_queue (boto3 inside the function)", "count": 1, "event_sample": "{\"fn\": \"m50-esm-a44109-8666\", \"event\": {\"Records\": [{\"messageId\": \"d01f58f5-f79e-447b-b8af-1e93b3316131\", \"receiptHandle\": \"... | **PASS** {"fired": {"via": "signal_queue (boto3 inside the function)", "count": 1, "event_sample": "{\"fn\": \"m50-esm-a44109-5146\", \"event\": {\"Records\": [{\"messageId\": \"bfe59f3c-67e6-44d8-9572-9b3503832542\", \"receiptHandle\": \"... |
| `lambda.runtime_fidelity` | **PASS** {"python": "3.12.0", "impl": "RustPython 0.6.0 with rustc 1.99.1 (58cf", "canary_env": null, "env_count": 24, "endpoint_env": null, "undeclared_import_requests": "ModuleNotFoundError", "host_fs_read": "OSError(38, 'a Lambda functi... | **WRONG** requested python3.12, ran 3.13.18; emulator process env var M50_CANARY leaked into the function; 'requests' importable without packaging it (AWS: ImportModuleError) / {'python': '3.13.18', 'impl': 'Clang 23.1.3 ]', 'canary_env': '... |
| `lambda.logs` | **PASS** {"LogResult_tail_has_REPORT": true, "tail_sample": "START RequestId: ffb7e118-6338-46b0-bb7f-e36e9c99c076 Version: $LATEST
MARK50_EVENT {\"x\": 1}
END RequestId: ffb7e118-6338-46b0-bb7f-e36e9c99c076
REPORT RequestI", "cloudwatc... | **WRONG** LogType=Tail has handler output but no START/END/REPORT lines / {'LogResult_tail_has_REPORT': False, 'tail_sample': 'MARK50_EVENT {"x": 1}', 'cloudwatch_log_lines': 1, 'cloudwatch_error': [], 'cloudwatch_lines_when_role_lacks_logs... |
| `lambda.iam_enforced_inside_function` | **PASS** {"error": "ClientError('An error occurred (AccessDenied) when calling the ListBuckets operation: User: arn:aws:sts::000000000000:assumed-role/m50-bare-a44109-479b/m50-iam-a44109-f0eb is not authorized to perform: s3:ListAllMyBucke... | **WRONG** role without policies could call s3:ListAllMyBuckets / {'list_buckets': 6} |
| `s3.presigned_sigv2_default` | **WRONG** {'query_style': 'SigV2 (AWSAccessKeyId=)', 'status': 403, 'body': '<Error><Code>InvalidAccessKeyId</Code><Message>The AWS Access Key Id you provided does not exist in our records.</Message><RequestId>000CSCWTTKSDHP86</RequestId><H... | **PASS** {"query_style": "SigV2 (AWSAccessKeyId=)", "status": 200, "body": "v2"} |
| `events.rule_to_sqs` | **PASS** {"delivered_without_queue_policy": 0, "FailedEntryCount": 0, "delivered": 1, "detail": [{"n": 10}], "test_event_pattern": true} | **PASS** {"delivered_without_queue_policy": 1, "FailedEntryCount": 0, "delivered": 1, "detail": [{"n": 10}], "test_event_pattern": true} |
| `scheduler.one_shot_to_sqs` | **PASS** {"expression": "at(2026-11-23T20:32:45)", "state": "ENABLED", "delivered": 1, "body": "{\"from\": \"scheduler\"}"} | **PASS** {"expression": "at(2026-11-23T20:32:12)", "state": "ENABLED", "delivered": 1, "body": "{\"from\": \"scheduler\"}"} |
| `sfn.execution` | **PASS** {"status": "SUCCEEDED", "output": {"in": 1, "data": {"n": 7}, "verdict": "big"}, "seconds": 1.51, "history_events": 10, "invalid_definition": {"http": 400, "code": "InvalidDefinition", "message": "Invalid State Machine Definition:... | **WRONG** {'status': 'SUCCEEDED', 'output': {'in': 1, 'data': {'n': 7}, 'verdict': 'big'}, 'seconds': 1.51, 'history_events': 10, 'invalid_definition': 'ACCEPTED (AWS: InvalidDefinition)'} |
| `sfn.test_state` | **PASS** {"status": "SUCCEEDED", "output": "{\"x\":42}"} | **PASS** {"status": "SUCCEEDED", "output": "{\"x\": 42}"} |
| `kms.roundtrip` | **PASS** {"cipher_len": 78, "decrypt_keyid_echo": "arn:aws:kms:us-east-1:000000000000:key/042d34cd-beda-4163-a6e4-8c01852d2e06", "decrypt_disabled_key": {"http": 400, "code": "DisabledException", "message": "arn:aws:kms:us-east-1:000000000... | **PASS** {"cipher_len": 96, "decrypt_keyid_echo": "arn:aws:kms:us-east-1:000000000000:key/1ea70fa2-6204-48ea-a880-955e382f04b3", "decrypt_disabled_key": {"http": 400, "code": "DisabledException", "message": "arn:aws:kms:us-east-1:000000000... |
| `secrets.create_get_rotate` | **PASS** {"value_ok": true, "stages": ["AWSCURRENT"], "rotate_without_lambda_permission": {"http": 400, "code": "AccessDeniedException", "message": "Secrets Manager cannot invoke the specified Lambda function. Ensure that the function poli... | **PASS** {"value_ok": true, "stages": ["AWSCURRENT"], "rotate_without_lambda_permission": "ACCEPTED", "rotation_enabled": true, "rotation_rules": {"AutomaticallyAfterDays": 30}, "missing_secret_error": {"http": 400, "code": "ResourceNotFou... |
| `ssm.securestring` | **PASS** {"decrypted_ok": true, "undecrypted_differs": true, "raw_prefix": "ASRjYzBhOTM3ZS02ZGM3LTQ1", "by_path_count": 1} | **PASS** {"decrypted_ok": true, "undecrypted_differs": true, "raw_prefix": "ENCRYPTED:czNjcjN0", "by_path_count": 1} |
| `sts.caller_identity` | **PASS** {"Account": "000000000000", "Arn": "arn:aws:iam::000000000000:user/emulator", "UserId": "AIDAH5I7GEMO7ADXNFAZU"} | **PASS** {"Account": "000000000000", "Arn": "arn:aws:iam::000000000000:root", "UserId": "000000000000"} |
| `iam.deny_policy_enforced` | **PASS** {"assumed_key_prefix": "EMULATOR", "list_buckets_with_deny_all": {"http": 403, "code": "AccessDenied", "message": "User: arn:aws:sts::000000000000:assumed-role/m50-deny-a44109-b954/m50 is not authorized to perform: s3:ListAllMyBuc... | **WRONG** explicit Deny * not enforced / {'assumed_key_prefix': 'ASIA778A', 'list_buckets_with_deny_all': 'ALLOWED', 'simulate_principal_policy': ['explicitDeny']} |
| `ses.send_and_inspect` | **WRONG** send OK but no way found to inspect sent mail / {'message_id': '010035e061dbef96-6fdf8bf3-6297-4808-927a-f845fca4865b-000000', 'seconds_until_identity_Success': 3.04, 'unverified_sender': {'http': 400, 'code': 'MessageRejected', '... | **PASS** {"message_id": "e29fec07-14ae-477a-ad4a-497e3472a922@email.amazonses.com", "seconds_until_identity_Success": 0.0, "unverified_sender": {"http": 400, "code": "MessageRejected", "message": "Email address is not verified. The followi... |
| `athena.select_1` | **PASS** {"state": "SUCCEEDED", "reason": null, "rows": [["one", "s"], ["1", "x"]]} | **PASS** {"state": "SUCCEEDED", "reason": "", "rows": [["one", "s"], ["1", "x"]]} |
| `bedrock.converse` | **PASS** {"text": "No language model runs in this emulator. anthropic.claude-3-haiku-20240307-v1:0 generated no text for this request. The request carried 1 message(s) and 2 input token(s).", "stopReason": "end_turn", "usage": {"inputToken... | **PASS** {"text": "[ministack mock anthropic anthropic.claude-3-haiku-20240307-v1:0] reply for prompt#deb43b5b", "stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 22, "totalTokens": 23}} |
| `logs.put_get` | **PASS** {"events": 1} | **PASS** {"events": 1} |
| `kinesis.put_get` | **PASS** {"records": 1} | **PASS** {"records": 1} |
| `apigw.http_api_lambda_proxy` | **PASS** {"ApiEndpoint": "https://127.0.0.1:9301/_aws/execute-api/mp67t4dbd4", "ApiId": "mp67t4dbd4", "before_add_permission": [500, "{\"message\":\"Internal Server Error\"}"], "tries": [["https://127.0.0.1:9301/_aws/execute-api/mp67t4dbd4... | **WRONG** quick-create Target ignored (no route/stage created); works after explicit integration+route+stage at http://127.0.0.1:4601/_aws/execute-api/<id>/ / {'ApiEndpoint': 'http://da133b5d.execute-api.localhost:4601', 'ApiId': 'da133b5d'... |
| `athena.query_s3_csv` | **PASS** {"rows": [["device", "total"], ["dev-1", "30"], ["dev-2", "5"]], "implicit_default_db": "ok"} | **WRONG** aggregate over S3 CSV wrong / {'rows': [['result'], ['mock_value']], 'implicit_default_db': 'CREATE EXTERNAL TABLE m50_a44109_d (a string) LOCATION \'s3:/ -> {\'State\': \'FAILED\', \'StateChangeReason\': "Schema \'default\' does ... |
| `lambda.esm_ddb_stream` | **PASS** {"via": "signal_queue (boto3 inside the function)", "count": 1, "event_sample": "{\"fn\": \"m50-dsm-a44109-59eb\", \"event\": {\"Records\": [{\"eventID\": \"b5c8261406e574cc077a43555cc72337\", \"eventName\": \"INSERT\", \"eventVer... | **PASS** {"via": "signal_queue (boto3 inside the function)", "count": 1, "event_sample": "{\"fn\": \"m50-dsm-a44109-0fa7\", \"event\": {\"Records\": [{\"eventID\": \"c6256653-cba6-4edc-b464-0a9110fcba44\", \"eventName\": \"INSERT\", \"even... |
| `sfn.lambda_task` | **PASS** {"status": "SUCCEEDED", "output": {"ok": true, "my_var": "hello-emu", "fn": "m50-sfl-a44109-a246", "python": "3.12.0", "marker": null, "aws_region_env": "us-east-1", "endpoint_env": null}} | **PASS** {"status": "SUCCEEDED", "output": {"ok": true, "my_var": "hello-ms", "fn": "m50-sfl-a44109-4f1e", "python": "3.13.18", "marker": null, "aws_region_env": "us-east-1", "endpoint_env": "http://127.0.0.1:4601"}} |
| `lambda.nodejs_invoke` | **PASS** {"payload": {"ok": true, "node": "v20.0.0", "v": "n"}, "FunctionError": null} | **WRONG** requested nodejs20.x, ran v22.23.5 / {'payload': {'ok': True, 'node': 'v22.23.5', 'v': 'n'}, 'FunctionError': None} |

Totals: AWS Emulator {'PASS': 42, 'WRONG_SEMANTICS': 2} / MiniStack {'PASS': 33, 'WRONG_SEMANTICS': 11} (run ba514f, 2026-11-23T20:33:21Z, boto3 1.43.124)
<!-- TABLE END -->

### MiniStack variants
| Check | MiniStack default | MiniStack `AUTH=true` | MiniStack + DuckDB |
|---|---|---|---|
| `iam.deny_policy_enforced` | WRONG (allowed) | PASS (`... with an explicit deny in an identity-based policy`) | n/a |
| `lambda.iam_enforced_inside_function` | WRONG (allowed) | PASS (`... because no identity-based policy allows ...`) | n/a |
| `s3.presigned_url` tampered signature | WRONG (200) | PASS (`SignatureDoesNotMatch`) | n/a |
| S3->SQS config without queue policy | accepted | rejected `InvalidArgument` | n/a |
| SNS->SQS without queue policy | delivered 1 | delivered 0 | n/a |
| EventBridge->SQS without queue policy | delivered 1 | delivered 1 (still WRONG) | n/a |
| S3->Lambda without AddPermission | accepted | accepted (still WRONG) | n/a |
| Secrets rotation without function policy | accepted | accepted (still WRONG) | n/a |
| `athena.query_s3_csv` | WRONG (mock rows) | WRONG (mock rows) | PASS (with Glue DB; implicit `default` DB fails) |

Totals with `AUTH=true`: 34 PASS / 7 WRONG of 41 checks (vs 31/10 default at that point).

### AWS Emulator with `--auth off`
Any access key accepted; presigned URLs are no longer validated (tampered and expired both 200); IAM on assumed roles still enforced (deny and missing-permission both `AccessDenied`). Evidence: `logs/emu-authoff-results.txt`.

## Ergonomics (measured, `ergonomics.py`)
| Item | AWS Emulator | MiniStack |
|---|---|---|
| Startup to healthy (3 runs) | 346 / 338 / 303 ms | 396 / 251 / 250 ms |
| Idle RSS 3 s after start | 323 / 328 / 327 MB | 41 / 41 / 41 MB |
| RSS after full probe run | 860 MB | 117 MB + 15 Lambda workers 573 MB |
| Reset without restart | none (`/__admin/reset` 404; docs: restart for clean state) | `POST /_ministack/reset` -> `{"reset": "ok"}` in 8 ms, 3 buckets -> 0 |
| State across restart | gone (in-memory only) | gone by default; `PERSIST_STATE=1` keeps SQS messages + DynamoDB items but **not S3 object bodies** (`NoSuchKey`); add `S3_PERSIST=1` and objects survive |
| Credentials across restart | rotate every start; old key -> `InvalidAccessKeyId`; re-read `~/.aws/emulator/<port>/emulator_credentials` or run `-a off` | any key, always |
| Inspect sent email | no route found (SES send works; only `GetSendStatistics`) | `GET /_ministack/ses/messages` |
| Async realism | DynamoDB table CREATING->ACTIVE ~3 s; SES identity Pending->Success ~3 s; TTL status `ENABLING` | immediate ACTIVE / Success / `ENABLED` |

## Cross-emulator (`probe.py --cross`)
- Same name, same ARN in both (`arn:aws:sqs:us-east-1:000000000000:m50-xq-...`). Nothing in either resolves an ARN against the other process.
- AWS Emulator bucket notification -> queue that exists only in MiniStack: `InvalidArgument: Unable to validate the following destination configurations`.
- MiniStack bucket notification -> queue that exists only in AWS Emulator: `InvalidArgument: Unable to validate the following destination configurations: arn:aws:sqs:us-east-1:000000000000:m50-xq2-...` (MiniStack checks existence even without AUTH).
- Same queue name created in both, notification configured in AWS Emulator: delivered to the AWS Emulator copy (1), MiniStack copy got 0.
- Bridge that works: a **MiniStack** Lambda (host subprocess, has network) calling the AWS Emulator over HTTPS with the emulator's CA and keys delivered `bridge-from-ministack-lambda` into an emulator queue.
- Bridge that cannot work: an **AWS Emulator** Lambda cannot reach MiniStack (`... addresses no service this emulator serves: ...`). So any cross-emulator hop must run in MiniStack or in host-side glue code, never inside an emulator Lambda.

## Published fidelity numbers
AWS Emulator docs: scores are per service, "fidelity tests passed out of total", published with each release (https://docs.aws.amazon.com/emulator/latest/userguide/fidelity.html, https://docs.aws.amazon.com/emulator/latest/userguide/what-is-service.html). The releases page (https://github.com/aws/aws-emulator/releases) shows v1.0.322 (commit 7753983, scored 2026-11-11) as latest; **1.0.589 is not published there**. The numbers live in the asset https://github.com/aws/aws-emulator/releases/download/v1.0.322/scores.json.gz (42,937 units: 32,402 behavioral + 10,535 wire; no per-service summary). Aggregation below is ours: "all" = units whose services include it (multi-service workflows count for each service), "solo" = units touching only that service; skipped units excluded. Treat as indicative for 1.0.589.

| Service | all pass/total | solo pass/total |
|---|---|---|
| S3 | 1763/2798 (63%) | 965/1220 (79%) |
| DynamoDB | 1153/1686 (68%) | 807/1044 (77%) |
| DynamoDB Streams | 26/40 (65%) | 16/25 (64%) |
| SQS | 1032/1514 (68%) | 299/333 (90%) |
| SNS | 440/635 (69%) | 210/284 (74%) |
| Lambda | 1561/2983 (52%) | 365/787 (46%) |
| EventBridge | 690/964 (72%) | 232/279 (83%) |
| Scheduler | 96/188 (51%) | 75/137 (55%) |
| Step Functions | 687/978 (70%) | 432/589 (73%) |
| KMS | 360/501 (72%) | 291/310 (94%) |
| Secrets Manager | 300/440 (68%) | 173/219 (79%) |
| SSM | 178/741 (24%) | 92/452 (20%) |
| STS | 121/246 (49%) | 94/169 (56%) |
| IAM | 709/1221 (58%) | 488/691 (71%) |
| SES | 106/153 (69%) | 106/141 (75%) |
| SESv2 | 141/254 (56%) | 105/164 (64%) |
| Athena | 92/457 (20%) | 50/343 (15%) |
| Bedrock (control) | 155/235 (66%) | 128/166 (77%) |
| Bedrock Runtime | 77/182 (42%) | 49/114 (43%) |
| CloudWatch Logs | 701/1204 (58%) | 466/642 (73%) |
| Kinesis | 235/405 (58%) | 151/249 (61%) |
| API Gateway v1 | 762/1209 (63%) | 579/780 (74%) |
| API Gateway v2 | 195/399 (49%) | 156/277 (56%) |
| All units | 24243/42857 (57%) | |

Evaluation suite: https://github.com/aws/aws-emulator-evaluation-suite (`manifest.jsonl`, 31,449 checks across 1,314 service labels; e.g. s3 539, dynamodb 1156, lambda 608, stepfunctions 545 single-label checks). An operation missing from the manifest is untested, not necessarily unimplemented; we hit no HTTP 501 in any check.

MiniStack: README (https://github.com/ministackorg/ministack) claims "60+ AWS services on a single port", "~30MB RAM at idle", startup "under 2 seconds", MIT; no per-service pass numbers found in the README. The 1.5.44 banner says `Ready — 91 services available`. README says Athena runs real SQL via DuckDB in the `:full` image only.

## Recommended placement
| Service / concern | Put it on | Reason (evidence) |
|---|---|---|
| IAM least-privilege of your Lambda roles, resource policies on queues/functions | **AWS Emulator** | Enforces identity policies for assumed roles and validates S3/SNS/EventBridge/Secrets/API GW permissions where AWS bites. MiniStack default allows everything; `AUTH=true` covers some but not EventBridge, S3->Lambda, Secrets rotation. Neither is a security test (AWS docs say so). |
| S3 (presigned URLs, multipart edges, notifications) | **AWS Emulator** (path-style, s3v4) | Validates signatures/expiry, `EntityTooSmall`, notification destinations. MiniStack accepts tampered presigned URLs and 1 KiB parts. Configure path-style + SigV4 explicitly for the emulator. |
| DynamoDB (conditions, transactions, GSI, streams, pagination) | either; **AWS Emulator** for error-path tests | Both identical on all functional checks and most error strings; MiniStack accepted `UpdateExpression="SET = 1"`. Emulator models CREATING->ACTIVE (~3 s), which catches missing waiters. |
| SQS / SNS / EventBridge / Scheduler data flow | either; AWS Emulator if policies matter | All delivery semantics matched (FIFO dedup, DLQ after maxReceiveCount=2, long polling, filter policies, numeric patterns, one-shot schedule). |
| Step Functions | **AWS Emulator** for definition validation; either for execution | Both ran Pass/Choice/Wait and a Lambda task to SUCCEEDED; MiniStack accepted an invalid definition. |
| Lambda: pure-Python/Node handlers that only call AWS | **AWS Emulator** | Runtime version as requested, isolated env, undeclared deps fail like AWS, IAM enforced, log tail correct. |
| Lambda: handlers needing native deps (numpy, pydantic-core, psycopg), third-party HTTP, or a real interpreter | **MiniStack** | Emulator Lambdas are RustPython with no network or C extensions. MiniStack runs real CPython/Node with full network. Watch: wrong runtime version, env leak, host packages. |
| Cross-emulator glue | **MiniStack Lambda or host code** | Only MiniStack Lambdas can reach the other emulator. |
| Athena | **AWS Emulator** (pip MiniStack fakes results) | Emulator executed real SQL over S3. Pip MiniStack returns fake rows with SUCCEEDED; MiniStack `:full`/DuckDB works if you create a Glue DB. |
| Bedrock | **MiniStack** with `MINISTACK_BEDROCK_PROXY_URL` (Ollama etc.) | Without a proxy both return canned text: Emulator `No language model runs in this emulator...`; MiniStack `[ministack mock anthropic ...] reply for prompt#...`. Only MiniStack can be wired to a real local model (proxy itself not exercised here). |
| SES | **MiniStack** for asserting emails | `/_ministack/ses/messages` exposes sent mail; Emulator has no inspection route. Emulator does model identity verification delay. |
| KMS / Secrets Manager / SSM SecureString / STS | either | All round trips matched. |
| API Gateway HTTP API quick-create | **AWS Emulator** | Emulator served it at `https://127.0.0.1:<port>/_aws/execute-api/<id>/`; MiniStack ignores quick-create `Target` and needs explicit integration+route+stage. |
| Fast test loops, CI isolation, persistence | **MiniStack** | 8 ms reset endpoint, 41 MB idle, optional persistence; Emulator needs restart + credential reload per clean state. |

## Surprises (exact strings)
- AWS Emulator, presigned URL generated without `signature_version=s3v4` (botocore default is SigV2 query auth `AWSAccessKeyId=...&Signature=...`): `403 <Error><Code>InvalidAccessKeyId</Code><Message>The AWS Access Key Id you provided does not exist in our records.</Message>`. Misleading: the key is fine, the signing version is the problem (rejecting SigV2 itself is reasonable; S3 dropped SigV2 for buckets created after June 2020). Using the emulator's generated `AWS_CONFIG_FILE` avoids it.
- AWS Emulator Lambda calling S3 with an unpolicied role: `AccessDenied ... User: arn:aws:sts::000000000000:assumed-role/<role>/<function> is not authorized to perform: s3:ListAllMyBuckets because no identity-based policy allows the s3:ListAllMyBuckets action`.
- AWS Emulator ESM: `InvalidParameterValueException: The function execution role does not have permissions to call ReceiveMessage on SQS`.
- AWS Emulator Scheduler: `ValidationException: The execution role you provide must allow AWS EventBridge Scheduler to assume the role.`
- AWS Emulator Secrets rotation: `AccessDeniedException: Secrets Manager cannot invoke the specified Lambda function. Ensure that the function policy grants access to the principal secretsmanager.amazonaws.com.`
- AWS Emulator SES immediately after `VerifyEmailIdentity`: `MessageRejected: Email address is not verified. The following identities failed the check in region US-EAST-1: sender@example.com` (identity is `Pending` for ~3 s, then `Success`). Unverified recipients were accepted.
- AWS Emulator Lambda: `OSError: [Errno 38] a Lambda function in this emulator has no host filesystem to reach` (even `platform.platform()` and `os.listdir("/")` raise it).
- AWS Emulator Lambda HTTP to a non-AWS host: `gaierror('https://example.com/ addresses no service this emulator serves: sqs, sns, sts, iam, events, eventbridge, pipes, scheduler, dynamodb, logs, ... transfer')`.
- AWS Emulator DynamoDB TTL: expired item (exp 1 h in the past) not returned by `GetItem` 3 s later. AWS deletes lazily and can still return it, so the emulator is stricter than AWS here.
- MiniStack Lambda env contained our canary `M50_CANARY=host-secret-canary` and 261 other host variables (in this workspace that included proxy and agent settings). Anything exported in the shell that starts MiniStack is visible to every function.
- MiniStack Athena without DuckDB: `SUCCEEDED` with rows `[['result'], ['mock_value']]` for a `GROUP BY` over S3 data. With DuckDB: `Schema 'default' does not exist` until a Glue database exists; `CREATE DATABASE` via Athena SQL fails with `Parser Error: syntax error at or near "DATABASE"` (DuckDB parser).
- MiniStack API GW v2 quick-create (`create_api(..., Target=<lambda arn>)`): no routes, no stages; every URL 404 `{"message":"Not Found"}`; `ApiEndpoint` is `http://<id>.execute-api.localhost:4601`, which does not resolve here (`Name or service not known`); use `http://127.0.0.1:4601/_aws/execute-api/<id>/` or a `Host:` header.
- MiniStack `LogType=Tail` returns only handler output, no `START/END/REPORT` lines. Logs still land in CloudWatch Logs even when the role has no `logs:*` permission.
- MiniStack S3 multipart accepted a 1 KiB non-final part (AWS: `EntityTooSmall: Your proposed upload is smaller than the minimum allowed size`, which the emulator returns).
- MiniStack DynamoDB accepted `UpdateExpression="SET = 1"`; AWS Emulator: `ValidationException: 1 validation error detected: Invalid UpdateExpression: Syntax error; token: "=", near: "SET = 1"`.
- MiniStack Step Functions accepted `{"StartAt": "X", "States": {}}`; AWS Emulator: `InvalidDefinition: Invalid State Machine Definition: 'MISSING_TRANSITION_TARGET: Missing 'Next' target: X at /StartAt, SCHEMA_VALIDATION_FAILED: Value cannot be empty at /States'`.
- MiniStack `PERSIST_STATE=1` alone: bucket metadata survives restart but the object is gone (`NoSuchKey`); `S3_PERSIST=1` is a separate switch.
- Both: S3 sends `s3:TestEvent` to the queue when the notification is configured (matches AWS; filter it out in consumers).
- Probe gotcha (ours): `pkill -f "aws-emulator -p 9301"` killed the calling shell because the pattern appears in its own command line; anchor it (`^aws-emulator -p 9301`).

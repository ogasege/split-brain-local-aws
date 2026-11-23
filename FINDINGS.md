# Findings: building a split local AWS on MiniStack + AWS Emulator

Lab notebook from building this repo on 23 November 2026 (Ubuntu 24.04,
aws-emulator 1.0.589, ministack 1.5.44, Terraform 1.17.2, AWS CLI 2.42.1,
boto3 1.43.124). Every entry was hit for real; error text is copied verbatim.
The side-by-side probe of both tools is in
[docs/compatibility/MATRIX.md](docs/compatibility/MATRIX.md).

## F1. AWS CLI v1 ignores per-service endpoints for SQS

With CLI 1.46.3 and `AWS_ENDPOINT_URL_SQS` set, `aws sqs list-queues` failed with:

```
SSL validation failed for https://queue.amazonaws.com/ [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain (_ssl.c:1035)
```

The signed request was going to **real AWS**; it only failed because the CA
bundle pointed at the emulator's CA. Cause: `awscli/customizations/overridesslcommonname.py`
forces legacy endpoints for sqs, emr, rds, docdb, neptune and health, overriding
both env vars and config-file `endpoint_url`.
**Fix:** AWS CLI v2 (2.42.1 works). `scripts/env.sh` refuses to load with v1.

## F2. Both SDK routing mechanisms work

`AWS_ENDPOINT_URL_<SERVICE>` environment variables and the shared-config
`services` section behaved identically in CLI v2 and boto3. Env vars are the
primary path here because the same variables can be injected into a Lambda.
The suffix is the botocore service id (`CLOUDWATCH_LOGS`, `SECRETS_MANAGER`,
`EVENTBRIDGE`), not the CLI command name.

## F3. AWS Emulator credentials are per-run

New keys on every start; `~/.aws/emulator/<port>/` is emptied on stop. With
`--auth off` no credentials file is written at all.

## F4. A MiniStack Lambda cannot sign for AWS Emulator (auth on)

```
An error occurred (AccessDenied) when calling the GetParameter operation: Sign the request with the emulator credentials
```

MiniStack sets `AWS_ACCESS_KEY_ID=000000000000` inside functions and strips
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN` from the
function's variables as reserved names.
**Fix:** run AWS Emulator with `--auth off`, static `test`/`test` keys for both.
**Tradeoff:** no signature checks locally, and IAM is not evaluated for calls
arriving from MiniStack.

## F5. One CA bundle covers both

MiniStack is plain HTTP, so `AWS_CA_BUNDLE=~/.aws/emulator/9297/ca.pem` is
enough. The certificate is valid only for `localhost`, `127.0.0.1`, `::1`.
A Lambda without it:

```
SSLError ... SSL validation failed for https://127.0.0.1:9297/ ... self-signed certificate (_ssl.c:1035)
```

**Fix:** inject `AWS_CA_BUNDLE` into the function environment.

## F6. Cross-emulator S3 notifications are refused

```
InvalidArgument ... Unable to validate the following destination configurations
```

Emulator log: `Not authorized to invoke function [arn:...:function:ingest]`.
**Rule:** event source and target share an emulator.

## F7. MiniStack Lambdas inherit MiniStack's process environment

A function's environment dump contained the starting shell's
`AWS_SECRET_ACCESS_KEY` (matched by hash, not printed), `HTTPS_PROXY` and the
shell's CA bundle path.
**Fix:** `scripts/up.sh` starts MiniStack under `env -i` with an explicit
variable list.

## F8. AWS Emulator Lambdas are sealed

RustPython 0.6.0 with a built-in boto3 (reports 1.0.0). `_sqlite3`, `_decimal`
and numpy are unavailable. HTTP to MiniStack fails with
`... addresses no service this emulator serves: sqs, sns, ...`. A boto3 client
created with a MiniStack `endpoint_url` silently listed the emulator's own
queues.

## F9. Without routing env the Lambda fails late, in the wrong emulator

```
ResourceNotFoundException ... PutItem ... Requested resource not found
```

The first handler fell back to default thresholds when the SSM parameter was
missing, hiding the wrong endpoint for one more step. It now fails fast.

## F10. Idempotency across emulators works

Replay of the same readings: `written: 0, duplicates: 13, alerts: 0`.

## F11. One Terraform provider reaches both emulators

Per-service `endpoints`, `custom_ca_bundle`, `s3_use_path_style` and the skip
flags; no provider aliases needed. SQS queues take about 25 s to create and
42 s to destroy (the AWS provider's own SQS waits).

## F12. DynamoDB number normalisation

`17.490` is stored as `17.49`. That is AWS behaviour, reproduced by the
emulator; the verify expectation was fixed.

## F13. Lambda INFO logs missing on MiniStack

No handler on Python's root logger outside real Lambda.
**Fix:** `logging.basicConfig()` when no handler exists.

## F14. Perpetual Terraform diff

`maximum_event_age_in_seconds = 21600 -> null`: MiniStack reports 21600 when
unset. **Fix:** set it explicitly.

## F15. DynamoDB parity for this app

Conditional put, transactions and number overflow behaved the same on both.
Overlapping update paths: AWS Emulator rejects like AWS, MiniStack accepts.

## F16. The handler also runs entirely inside AWS Emulator

Fails with no role policy
(`AccessDenied ... no identity-based policy allows the s3:GetObject action`),
fails right after CreateTable while the table is `CREATING`
(`ResourceNotFoundException`), then succeeds once `ACTIVE`.

## F17. S3 addressing on AWS Emulator

boto3 picks path-style automatically for an IP endpoint. Virtual-hosted style
with `localhost` fails:
`EndpointConnectionError ... https://style-probe.localhost:9297/...`.
Terraform sets `s3_use_path_style = true`.

## Not verified

- docker-compose mode (no Docker daemon in the build environment; only
  `docker compose config` validated).
- `LAMBDA_EXECUTOR=docker`, and MiniStack `AUTH=true` with this stack.
- The emulator download URL is `LTS/latest`; versioned URLs returned 403, so CI
  warns when the installed version is not 1.0.589.

# One Local AWS, Two Emulators: Routing Services Between AWS Emulator and MiniStack

*Run AWS Emulator and MiniStack side by side, send each AWS service to the emulator that handles it best, and drive both from the AWS CLI, boto3, one Terraform provider and the Lambda runtime. Every claim in this article comes from code I ran on 23 November 2026.*

- **Repo:** https://github.com/ogasege/split-brain-local-aws
- **Tested with:** AWS Emulator 1.0.589, MiniStack 1.5.44, Terraform 1.17.2 (AWS provider 6.73.0), AWS CLI 2.42.1, boto3, Python 3.12/3.13, Ubuntu 24.04
- **Cost:** $0. Nothing here touches a real AWS account, including CI.
- **Level:** intermediate. You should be comfortable with the AWS CLI, Terraform and some Python.

## Why two emulators

In March 2026 LocalStack folded its free Community image into a single image that requires an auth token (there's a free Hobby plan for non-commercial use). Two free tools now fill that gap:

- **AWS Emulator**, released by AWS on 11 November 2026. It's an official local implementation of the AWS APIs, scored service by service against real AWS behaviour.
- **MiniStack**, an MIT-licensed community emulator built as a drop-in LocalStack replacement. It runs your code on real interpreters and ships with endpoints for test automation.

The workload I care about locally is the kind of pipeline I owned at AMMP: devices drop files, a Lambda parses them, state lands in DynamoDB, alerts go out. When I tried each emulator on that shape, neither one covered everything:

- AWS Emulator returned AWS-exact errors and caught missing permissions, but its Lambda runtime couldn't import a package with a C extension.
- MiniStack ran my handler on real CPython, let my tests read sent emails and reset all state in milliseconds, but accepted requests real AWS rejects.

So I made them work as one local AWS. Each service goes to the emulator that is strongest for it, and the routing lives in one file that the CLI, the SDKs, Terraform and the Lambda runtime all read. By the end of this article you'll have both emulators running, 13 resources deployed across them with one `terraform apply`, and a 16-check end-to-end test passing.

## What each emulator is actually strong for

Before deciding any routing, I wrote a probe that runs the same boto3 calls against both emulators and records whether each result matches AWS. It's in the repo under `docs/compatibility/`, so you can rerun it when either tool ships a new version. Out of 44 checks per emulator, **AWS Emulator matched AWS on 42 and MiniStack on 33**. Neither returned a single `501 Not Implemented`.

The totals matter less than where each one differs:

| Concern | Better fit | What I measured |
|---|---|---|
| IAM on Lambda execution roles | AWS Emulator | A function whose role had no policies got `AccessDenied ... because no identity-based policy allows the s3:ListAllMyBuckets action`. MiniStack let the same call through. |
| Resource policies on wiring | AWS Emulator | An S3 notification to SQS without a queue policy failed with `InvalidArgument: Unable to validate the following destination configurations`. SNS or EventBridge to SQS without a queue policy delivered 0 messages. MiniStack accepted and delivered all of them. |
| Request validation | AWS Emulator | It rejected a 1 KiB multipart part (`EntityTooSmall`), a tampered presigned URL (`SignatureDoesNotMatch`, with auth on; with `--auth off` it accepts tampered and expired URLs), `UpdateExpression="SET = 1"` and an empty Step Functions definition. MiniStack accepted all four. |
| Athena | AWS Emulator | It ran real SQL over a CSV in S3. MiniStack installed from pip returned `SUCCEEDED` with fake rows (`[['result'], ['mock_value']]`). |
| Lambda with native dependencies | MiniStack | AWS Emulator runs Python Lambdas on an embedded RustPython (it reports `3.12.0`, implementation `RustPython 0.6.0`). C extensions don't load (`No module named '_ctypes'`), and there's no host filesystem and no network except AWS hosts. MiniStack runs real CPython subprocesses. |
| Asserting on side effects | MiniStack | `GET /_ministack/ses/messages` returns every email sent and `GET /_ministack/sqs/messages` peeks at queues without consuming. AWS Emulator accepts `SendEmail` but gives you no way to read what was sent. |
| Resetting between tests | MiniStack | `POST /_ministack/reset` wiped all state in 8 to 13 ms in my runs. AWS Emulator has no reset route, so you restart it, and its credentials rotate on every start. |
| Footprint | MiniStack | About 41 MB of memory idle, against about 325 MB for AWS Emulator. |
| SQS, SNS, EventBridge, Scheduler, KMS, Secrets Manager, STS | Either (except the resource-policy checks above) | FIFO dedup, DLQ redrive, long polling, filter policies, one-shot schedules and KMS round trips matched on both. |

Two of these surprised me.

First, AWS's launch post says the emulator doesn't evaluate IAM and resource policies the way AWS does and shouldn't be used for security testing. That's still the right rule. But in practice it *does* enforce identity policies on assumed-role credentials, which makes it very good at catching the missing `sqs:ReceiveMessage` or `lambda:AddPermission` that usually only shows up on your first real deploy.

Second, MiniStack has an `AUTH=true` switch that closes most of its IAM gap. With it on, it still accepted EventBridge to SQS without a queue policy, S3 to Lambda without a permission, and Secrets Manager rotation without a function policy.

AWS publishes raw fidelity test results with each release. Aggregating the 1.0.322 results by service (my counting; a test that touches several services counts for each) gives 57% overall, SQS 68%, DynamoDB 68%, S3 63%, Lambda 52% and SSM 24%. Read those as "how much has been verified so far", not as a grade.

## The architecture

![Architecture: routing.toml renders config for the CLI, SDKs and Terraform; MiniStack holds S3, Lambda, SQS and SES; AWS Emulator holds DynamoDB, SSM and the default route](https://prod-assets.cosmic.aws.dev/a/3MWXR1UySPMgVXkTPWO2jOT8K7O/spli.webp)

The sample app is a cut-down energy-telemetry pipeline. A site uploads a CSV of cumulative kWh meter readings. A Lambda turns those counters into interval energy (handling register rollovers and meter resets), writes each interval to DynamoDB with a conditional write so replays are idempotent, sends anomalies to an SQS queue and emails a digest.

Two rules decide the placement, and both came from things that broke:

1. **An event source and its target must live in the same emulator.** AWS Emulator refuses an S3 notification to a Lambda it doesn't host, and neither tool resolves an ARN that lives in the other process. So S3 and Lambda sit together.
2. **Calls can cross from MiniStack to AWS Emulator, never the other way.** AWS Emulator runs Lambda code in a sealed in-process sandbox that can only reach its own services (`addresses no service this emulator serves`). MiniStack runs functions as normal processes that can call anything you route them to. So the Lambda lives in MiniStack, and S3 has to follow it.

Here's the resulting routing:

| Service | Emulator | Why |
|---|---|---|
| lambda | MiniStack | Real CPython with your real dependencies, and its functions can call AWS Emulator |
| s3 | MiniStack | The upload bucket triggers the Lambda, so they must share an emulator |
| sqs, ses | MiniStack | Tests read alerts and sent mail through `/_ministack/*` endpoints |
| iam, logs | MiniStack | The execution role and log group belong where the function runs |
| dynamodb, ssm | AWS Emulator | Idempotency depends on exact condition and validation behaviour |
| sts, kms, secretsmanager | AWS Emulator | Unused by the app, but routed explicitly so they default to the fidelity-first emulator |

Is the split worth it for an app this small? I checked. The same zip also runs entirely inside AWS Emulator: I deployed it there with S3, DynamoDB, SSM and SQS all in the emulator, and once the role had a policy it wrote the same 13 rows and 3 alerts. The split earns its place when you need what only one side has: MiniStack's test hooks and native-dependency Lambdas, and AWS Emulator's validation and IAM behaviour. The pattern is the point. Swap the placement for your own app.

## Step 1: Install both emulators

On Linux, AWS Emulator ships as a package. MiniStack is on PyPI.

```bash
# Tools a stock Ubuntu 24.04 box is missing
sudo apt-get install -y make jq curl git python3-venv

# AWS Emulator (Debian/Ubuntu amd64; aws-emulator_arm64.deb and .rpm builds also exist, ~216 MB)
curl -fsSLo /tmp/aws-emulator.deb \
  https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator_amd64.deb
sudo apt-get install -y /tmp/aws-emulator.deb
aws-emulator --version     # 1.0.589 when I tested

# The repo, in a virtualenv (Ubuntu's system Python is "externally managed")
git clone https://github.com/ogasege/split-brain-local-aws.git
cd split-brain-local-aws
python3 -m venv .venv && . .venv/bin/activate   # keep it active for every make command
pip install -r requirements-dev.txt             # pins ministack==1.5.44, plus boto3 and pytest

aws --version              # must be AWS CLI v2, see "What broke"
terraform version          # 1.17.2 tested
```

You also need [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) (use the official installer; `pip install awscli` gives you v1, which breaks this setup) and [Terraform](https://developer.hashicorp.com/terraform/install).

On macOS and Windows, AWS Emulator has no prebuilt image, so you build one from the Dockerfile in its user guide. The repo includes that Dockerfile and a `docker-compose.yml` (see the README's compose mode). I built and tested on Linux, so treat compose mode as unverified until you've run it.

The `LTS/latest` URL isn't version-pinned. CI logs the installed version and warns when it isn't 1.0.589, so you notice when behaviour might change under you.

## Step 2: Start them side by side without collisions

`make up` runs `scripts/up.sh`. These are the two commands that matter (simplified: the script adds `nohup`, logging, pid files and absolute `.local` paths):

```bash
# AWS Emulator: HTTPS on 127.0.0.1:9297, signature checks off, telemetry off
env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 EMULATOR_TELEMETRY=off \
  aws-emulator --bind 127.0.0.1 --port 9297 --auth off --telemetry off

# MiniStack: HTTP on 127.0.0.1:4566, started with an EMPTY environment
env -i \
  PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 \
  GATEWAY_PORT=4566 BIND_HOST=127.0.0.1 MINISTACK_REGION=us-east-1 \
  AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test \
  LAMBDA_EXECUTOR=local IOT_MTLS_ENABLED=0 SFTP_ENABLED=0 \
  S3_DATA_DIR=.local/ministack/s3 STATE_DIR=.local/ministack/state \
  ministack
```

Every flag here is there because of something I hit:

- **`env -i` on MiniStack** is a security fix. Its local Lambda executor copies MiniStack's *own* process environment into every function. I started it from my normal shell once and dumped a function's environment: it contained my shell's `AWS_SECRET_ACCESS_KEY`, my proxy settings and my CA bundle. In a separate probe, a canary variable showed up in the function along with 261 other host variables. Starting MiniStack with a clean, explicit environment stops that. I start AWS Emulator the same way: its Lambdas are sandboxed, but there's no reason to hand the process your real credentials either.
- **`BIND_HOST=127.0.0.1`**, because MiniStack binds `0.0.0.0` by default and would otherwise be reachable from your network.
- **`IOT_MTLS_ENABLED=0`**, because MiniStack opens an MQTT listener on port 8883 at boot, which collides with a second instance or anything else on that port.
- **`--telemetry off`**, because AWS Emulator sends anonymous usage data by default.
- **`--auth off`**, which needs its own section.

Run `make up` now. The script waits for both health checks (`https://127.0.0.1:9297/__admin/health` and `http://127.0.0.1:4566/_ministack/health`); each emulator was healthy in about 300 ms on my machine.

## Step 3: Reconcile credentials and TLS

The two emulators disagree on two things.

**Credentials.** AWS Emulator normally accepts only its own `EMULATOR...` keys, which it regenerates on every start. MiniStack accepts any key. Using the emulator's keys everywhere works for the CLI and Terraform, then breaks the moment a Lambda running in MiniStack calls the emulator:

```
botocore.exceptions.ClientError: An error occurred (AccessDenied) when calling the
GetParameter operation: Sign the request with the emulator credentials
```

MiniStack injects its own credentials into every function and, like real Lambda, treats `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN` as reserved names. You can't pass the emulator's key in through the function's environment. The only way around it would be local-only credential code in the handler, which is exactly what I didn't want. So I start AWS Emulator with `--auth off`, and both emulators accept the same static `test`/`test` keys. MiniStack maps a non-numeric key to account `000000000000`, which is also AWS Emulator's only account, so ARNs line up across both.

What that costs you: with auth off, a broken signing setup isn't caught locally. And because the cross-emulator calls come from MiniStack, which doesn't evaluate IAM, **nothing in this split setup tests your IAM policies.** Test those against a real sandbox account, or run the function inside AWS Emulator as a separate check where its IAM enforcement applies.

**TLS.** AWS Emulator serves HTTPS with a CA it writes to `~/.aws/emulator/9297/ca.pem` on every start. MiniStack is plain HTTP. `AWS_CA_BUNDLE` is only consulted for HTTPS connections, so pointing it at the emulator's CA is enough for both. You don't need a combined bundle. The certificate is valid only for `localhost`, `127.0.0.1` and `::1`, which has two consequences: always use `127.0.0.1` in endpoints, and force path-style S3 addressing (a `<bucket>.localhost` request fails TLS verification).

## Step 4: Route every tool from one file

AWS CLI v2 and the current SDKs read a per-service endpoint from `AWS_ENDPOINT_URL_<SERVICE>`, which takes precedence over the global `AWS_ENDPOINT_URL`. That's the core mechanism. The rest of the work is making sure every tool reads the same values, so I keep them in one file:

```toml
# routing.toml (abridged)
region  = "us-east-1"
default = "aws-emulator"   # anything unlisted goes here, never to real AWS

[emulators.ministack]
endpoint = "http://127.0.0.1:4566"

[emulators.aws-emulator]
endpoint  = "https://127.0.0.1:9297"
ca_bundle = "~/.aws/emulator/9297/ca.pem"

[services.lambda]
to  = "ministack"
why = "Real CPython with your real dependencies; can call AWS Emulator."

[services.s3]
to  = "ministack"
why = "The bucket triggers the Lambda; source and target share an emulator."

[services.dynamodb]
to  = "aws-emulator"
why = "Idempotency depends on exact ConditionExpression semantics."
```

The fallback matters. Pointing `default` at an emulator, instead of leaving it unset, means a service you forgot to route can never reach a real account by accident.

`make routes` runs `tools/render_routes.py`, a stdlib-only script, which renders that file three ways:

| Output | Read by |
|---|---|
| `.local/routes.env` | AWS CLI v2 and the SDKs (`AWS_ENDPOINT_URL_<SERVICE>` exports) |
| `.local/aws-config` | The same routes as a shared-config `services` section |
| `terraform/provider.gen.tf` | The provider's `endpoints {}` block, plus a `routing_env` map injected into every Lambda |

Both SDK mechanisms, environment variables and the config-file `services` section, behaved identically in AWS CLI 2.42.1 and boto3. I use environment variables as the primary path because the exact same variables can be injected into a Lambda.

One trap: the variable suffix is the botocore *service ID*, upper-cased with spaces turned into underscores, not the CLI command name. CloudWatch Logs is `AWS_ENDPOINT_URL_CLOUDWATCH_LOGS` (not `_LOGS`), Secrets Manager is `AWS_ENDPOINT_URL_SECRETS_MANAGER` and EventBridge is `AWS_ENDPOINT_URL_EVENTBRIDGE`. The generator keeps that mapping in one table so you don't have to remember it.

To put your own shell on the split stack:

```bash
source scripts/env.sh
aws s3 ls                    # answered by MiniStack
aws dynamodb list-tables     # answered by AWS Emulator
python3 -c "import boto3; print(boto3.client('ssm').meta.endpoint_url)"
# https://127.0.0.1:9297
```

`scripts/env.sh` also unsets profiles and session tokens, points `AWS_SHARED_CREDENTIALS_FILE` at `/dev/null`, adds `127.0.0.1` to `NO_PROXY`, and refuses to load, before changing anything in your shell, if the `aws` on your `PATH` is CLI v1 (more on why below).

## Step 5: Point one Terraform provider at two emulators

The AWS provider already supports a separate endpoint per service, so one provider can manage resources in both emulators. This is the generated provider, abridged (the real file also lists kms and secretsmanager, and ends with the `routing_env` map used in Step 6):

```hcl
# terraform/provider.gen.tf (generated from routing.toml, do not edit)
provider "aws" {
  region     = "us-east-1"
  access_key = "test"
  secret_key = "test"

  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  s3_use_path_style           = true   # the emulator's cert has no wildcard

  custom_ca_bundle = "/home/you/.aws/emulator/9297/ca.pem"   # HTTP endpoints ignore it

  endpoints {
    dynamodb = "https://127.0.0.1:9297"
    ssm      = "https://127.0.0.1:9297"
    sts      = "https://127.0.0.1:9297"
    lambda   = "http://127.0.0.1:4566"
    s3       = "http://127.0.0.1:4566"
    sqs      = "http://127.0.0.1:4566"
    ses      = "http://127.0.0.1:4566"
    iam      = "http://127.0.0.1:4566"
    logs     = "http://127.0.0.1:4566"
    # ... kms and secretsmanager -> https://127.0.0.1:9297
  }
}
```

The resources in `terraform/main.tf` are ordinary Terraform with nothing emulator-specific in them: a bucket and its notification, the S3 invoke permission, the Lambda with its IAM role, policy, log group and async-invoke config (pointing failures at a dead-letter queue), the alerts queue, an SES identity, an SSM parameter and a DynamoDB table. `make deploy` applies all 13 across both emulators.

## Step 6: Tell the Lambda where everything lives

This is the step that cost me the most time. MiniStack injects `AWS_ENDPOINT_URL=http://127.0.0.1:4566` into every function, so without any extra configuration the function's SDK sends *every* call to MiniStack. My first deploy failed with:

```
ResourceNotFoundException | An error occurred (ResourceNotFoundException) when
calling the PutItem operation: Requested resource not found
```

The table existed. It was in the other emulator. The fix is to merge the rendered routing into the function's environment:

```hcl
resource "aws_lambda_function" "ingest" {
  function_name = "${var.name_prefix}-ingest"
  runtime       = "python3.12"
  handler       = "ingest.handler.handler"
  # ...
  environment {
    # routing_env comes from routing.toml: it points the function's own SDK
    # calls at the right emulator and trusts AWS Emulator's CA.
    # On a real account, drop it and the SDK uses normal AWS endpoints.
    variables = merge(local.routing_env, {
      TABLE_NAME      = aws_dynamodb_table.intervals.name
      ALERT_QUEUE_URL = aws_sqs_queue.alerts.url
      CONFIG_PARAM    = aws_ssm_parameter.telemetry_config.name
      # ... plus ALERT_EMAIL_FROM and ALERT_EMAIL_TO for the SES digest
    })
  }
}
```

`routing_env` contains the same `AWS_ENDPOINT_URL_<SERVICE>` values your shell uses, plus `AWS_CA_BUNDLE`. Without the CA bundle, the cross-emulator call fails with:

```
SSLError | SSL validation failed for https://127.0.0.1:9297/
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate
```

The handler itself has no emulator code. It creates plain `boto3.client("dynamodb")` clients and the environment decides where they go. My first version made the bug harder to find: it fell back to default thresholds when the SSM parameter wasn't found, which hid the wrong endpoint for one more step. It now fails fast when configuration is missing, which is better behaviour in production too.

Idempotency is one line of DynamoDB:

```python
# app/ingest/handler.py
ddb.put_item(
    TableName=TABLE_NAME,
    Item=to_item(iv, source),
    ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
)
```

S3 delivers events at least once and people re-upload files, so a replayed file has to produce zero new rows and zero new alerts. That's why DynamoDB sits on AWS Emulator: I want the condition semantics to match AWS exactly. On the condition checks this app depends on (conditional put, `ReturnValuesOnConditionCheckFailure`, transaction cancellation reasons), both emulators actually behaved identically. The differences showed up on validation paths, where MiniStack accepted an invalid `UpdateExpression` that AWS rejects.

## Step 7: Deploy and verify end to end

```bash
make up deploy verify
```

`scripts/verify.sh` uses a fresh site prefix per run so it never collides with earlier runs, uploads a sample CSV containing a rollover, a reset, a spike and a gap, then checks placement, results and idempotency. This is the real output from my run:

```
[split-brain] run id r179546641321065: 16 readings, 3 meters, 2 sites
[split-brain] placement
[ ok ] PASS  bucket split-brain-meter-uploads lives in MiniStack
[ ok ] PASS  bucket is NOT in AWS Emulator
[ ok ] PASS  table split-brain-meter-intervals lives in AWS Emulator
[ ok ] PASS  table is NOT in MiniStack
[ ok ] PASS  function split-brain-ingest carries the routing env from routing.toml
[split-brain] upload s3://split-brain-meter-uploads/incoming/r179546641321065/readings.csv
[ ok ] PASS  13 interval rows written to DynamoDB (AWS Emulator) within 45s
[ ok ] PASS  3 alerts on the SQS queue (MiniStack)
[ ok ] PASS  alert types are GAP, RESET, SPIKE
[ ok ] PASS  one alert digest email sent via SES (MiniStack)
[ ok ] PASS  counter rollover 99985.25 -> 2.75 stored as 17.49 kWh, quality ROLLOVER
[ ok ] PASS  meter reset stored with no kWh, quality RESET
[split-brain] replay: upload the same readings under a new key
[ ok ] PASS  replay processed: Lambda log shows 13 duplicates
[ ok ] PASS  still 13 rows after replay (conditional writes rejected duplicates)
[ ok ] PASS  still 3 alerts after replay (no duplicate alerts)
[ ok ] PASS  still one digest email after replay
[ ok ] PASS  malformed CSV produces one BAD_FILE alert

[ ok ] VERIFY PASS (16 checks, run r179546641321065)
```

The placement checks matter as much as the results. They prove the bucket and table really live where `routing.toml` says, so a routing mistake can't hide behind a passing functional test.

Then run the tests:

```bash
make test   # unit tests for the meter maths + integration tests against the stack
```

The integration suite checks that every route in `routing.toml` resolves correctly in boto3, that an unrouted service falls back to the emulator, that `ReturnValuesOnConditionCheckFailure` returns the old item, and that a cross-emulator S3 notification is refused.

Between test runs, `make reset` wipes both: MiniStack through `POST /_ministack/reset`, AWS Emulator by restarting it, since it has no reset route. Run `make deploy` again afterwards.

## Step 8: Run it in CI with no AWS account

The workflow in `.github/workflows/local-aws.yml` runs the whole thing on every push and pull request. An excerpt:

```yaml
- name: Install AWS Emulator (.deb)
  run: |
    curl -fsSLo /tmp/aws-emulator.deb \
      https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator_amd64.deb
    sudo apt-get install -y /tmp/aws-emulator.deb
    installed="$(aws-emulator --version | awk '/^Version/ {print $2}')"
    if [[ "$installed" != "$EXPECTED_EMULATOR_VERSION" ]]; then
      echo "::warning::aws-emulator $installed differs from tested $EXPECTED_EMULATOR_VERSION"
    fi

- name: Install MiniStack and test deps
  run: pip install "ministack==${MINISTACK_VERSION}" boto3 pytest

- run: make unit
- run: make up
- run: make deploy
- run: make verify
- run: make integration

- name: Destroy and stop
  if: always()
  run: |
    make destroy || true
    make down
```

No credentials, no secrets, no bill. The first run on GitHub's `ubuntu-latest` runner installed both emulators, deployed all 13 resources, passed the 16-check verify and the integration tests, and destroyed everything in 2 minutes 56 seconds ([run #1](https://github.com/ogasege/split-brain-local-aws/actions/runs/45262827331)). I pinned Python 3.12 in CI on purpose: MiniStack's local executor runs a `python3.12` function on whatever interpreter MiniStack itself runs on.

## What broke, and how I fixed it

| What I saw | Cause | Fix |
|---|---|---|
| `AccessDenied ... Sign the request with the emulator credentials` from the Lambda | MiniStack injects its own credentials and treats credential variables as reserved, so the function can't present the emulator's rotating key | Run AWS Emulator with `--auth off` and use static keys everywhere |
| `ResourceNotFoundException` on `PutItem` while the table existed | MiniStack points every SDK call in the function at itself | Inject `AWS_ENDPOINT_URL_<SERVICE>` routing into the function's environment |
| `SSL validation failed ... self-signed certificate` inside the Lambda | The function didn't trust the emulator's CA | Add `AWS_CA_BUNDLE` to the injected environment |
| My shell's `AWS_SECRET_ACCESS_KEY` visible inside a Lambda | MiniStack's local executor copies its own process environment into every function | Start MiniStack under `env -i` with an explicit variable list |
| `InvalidArgument: Unable to validate the following destination configurations` | The S3 notification's target was in the other emulator | Keep event sources and targets in the same emulator |
| `SSL validation failed for https://queue.amazonaws.com/` with routing exported | AWS CLI v1 ignores per-service endpoints for SQS in every region (and for RDS, DocumentDB, Neptune, EMR and Health in some regions) and sends those calls to real AWS | Use AWS CLI v2; `env.sh` refuses to load with v1 |
| Presigned URLs from AWS Emulator returned `InvalidAccessKeyId` | botocore defaults to SigV2 query signing for presigned URLs; the key was fine | Set `signature_version = s3v4` (the emulator's own generated config already does) |
| `ResourceNotFoundException` right after creating a table | AWS Emulator models the `CREATING` state for a few seconds, like AWS | Use a waiter. Terraform already waits for `ACTIVE` |
| Lambda INFO logs missing in CloudWatch Logs | Python's root logger has no handler outside the real Lambda runtime | Call `logging.basicConfig()` when no handler exists |
| `terraform plan` never clean: `maximum_event_age_in_seconds = 21600 -> null` | MiniStack reports 21600 when the value is unset | Set it explicitly |

The one I'd stress most is the CLI v1 row. Here it only failed because `AWS_CA_BUNDLE` trusted nothing but the emulator's CA. Without that, the signed request reaches AWS, and with real credentials loaded it runs against your account. Check `aws --version` before you trust any local setup.

## Limits, and what this setup is not

- **It isn't a security test.** In this split, IAM isn't evaluated at all. AWS Emulator enforces identity policies for its own Lambdas' roles, but AWS still says not to use it for security testing, and fellow builder Simi Mwanza found it ignores permissions boundaries and accepts malformed policies ([AWS Emulator vs Real AWS: 146 Steps, 17 Differences](https://builder.aws.com/content/3MQNBVXYVeIvcStOBpoVcLzPPkV/aws-emulator-vs-real-aws-146-steps-17-differences)). Test IAM on a real sandbox account before production.
- **MiniStack's local executor isn't sandboxed.** A `python3.12` function ran on Python 3.13 here, and packages installed on the host import even if they aren't in your zip, so a missing dependency won't fail locally. MiniStack's `LAMBDA_EXECUTOR=docker` mode uses real runtime images; I didn't have a Docker daemon to test it. Keep a packaging test either way.
- **S3 on MiniStack is looser than AWS.** It accepted a tampered presigned URL and a 1 KiB multipart part. If your app hands presigned upload URLs to devices, test that path against a separate AWS Emulator started with auth on, not the `--auth off` instance `make up` starts.
- **One account, one region.** AWS Emulator runs a single account (`000000000000`) per process.
- **State is in memory.** Both emulators lose everything when they stop. `make down` also deletes the Terraform state for that reason, so it never describes resources that no longer exist.
- **Fidelity varies by service and changes between releases.** Rerun the probe in `docs/compatibility/` for the services you depend on whenever you upgrade either tool.

## Clean up

```bash
make destroy   # terraform destroy across both emulators
make down      # stop both processes started by make up
make clean     # remove .local/, generated provider, .terraform/
```

There's nothing to clean up in AWS because nothing was created there.

## Wrapping up

The tools are new, but the idea is old: route each dependency to the implementation that is most faithful for the behaviour you're testing, and keep that decision in one place. The three pieces that make it work are a single routing file rendered for every tool, a rule that event sources and their targets share an emulator, and Lambdas that are told explicitly where each service lives.

Clone the repo, run `make up deploy verify`, then move one line in `routing.toml` and watch where your resources land. If you only adopt one thing from this article, make it the probe: running the same calls against both emulators showed me where each one was wrong before my app did.

**Repo:** https://github.com/ogasege/split-brain-local-aws

I also filed two Wishes from gaps I hit while building this. If you've run into them too, a vote helps:

- [AWS Emulator: publish a prebuilt multi-arch container image](https://builder.aws.com/wishlist/3MWU8ReYeSWu134HzonwtZt61Ry_w/aws-emulator-publish-a-prebuilt-multi-arch-container-image)
- [AWS Emulator: run Lambda functions on the real Lambda runtimes](https://builder.aws.com/wishlist/3MWVeXBeaw3wQP19GqB0jLwoMC3_w/aws-emulator-run-lambda-functions-on-the-real-lambda-runtimes)

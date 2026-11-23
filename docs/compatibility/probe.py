#!/usr/bin/env python3
"""Mark 50 probe: run IDENTICAL boto3 checks against AWS Emulator and MiniStack.

Usage:
  python3 probe.py                 # both targets, all checks
  python3 probe.py --target emu    # one target
  python3 probe.py --only s3.      # checks whose id starts with s3.
  python3 probe.py --cross         # cross-emulator checks only

Statuses:
  PASS              behaved like AWS for what we checked
  FAIL              call raised an error AWS would not raise (exact error recorded)
  NOT_IMPLEMENTED   HTTP 501 (or explicit not-implemented response)
  WRONG_SEMANTICS   call succeeded but the observable result differs from AWS
  TIMEOUT           check exceeded its time budget
  SKIP              precondition missing
Never talks to real AWS: every client gets an explicit endpoint_url and the
workspace AWS_* variables are removed from this process before boto3 loads.
"""
import os, sys

# ---- hard isolation from the workspace's real-AWS settings -----------------
for k in list(os.environ):
    if k.startswith("AWS_"):
        del os.environ[k]
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AWS_CONFIG_FILE"] = "/dev/null"
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"

import argparse, base64, concurrent.futures as cf, configparser, io, json, ssl
import threading, time, traceback, urllib.request, uuid, zipfile
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = uuid.uuid4().hex[:6]
MARKER_ROOT = f"/tmp/mark50-markers/{RUN}"
REGION = "us-east-1"
ACCOUNT = "000000000000"


# ---------------------------------------------------------------- targets ---
def target_config(name):
    if name == "emu":
        port = 9301
        d = os.path.expanduser(f"~/.aws/emulator/{port}")
        cp = configparser.ConfigParser()
        cp.read(os.path.join(d, "emulator_credentials"))
        return dict(name="emu", label="AWS Emulator", endpoint=f"https://127.0.0.1:{port}",
                    key=cp["default"]["aws_access_key_id"], secret=cp["default"]["aws_secret_access_key"],
                    verify=os.path.join(d, "ca.pem"))
    if name == "ms":
        return dict(name="ms", label="MiniStack", endpoint="http://127.0.0.1:4601",
                    key="test", secret="test", verify=False)
    raise ValueError(name)


class Target:
    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg["name"]
        self.endpoint = cfg["endpoint"]
        self.session = boto3.Session(aws_access_key_id=cfg["key"], aws_secret_access_key=cfg["secret"],
                                     region_name=REGION)
        self._clients = {}
        self.lock = threading.Lock()
        self.shared = {}  # cross-check scratch (e.g. lambda arn)

    def c(self, svc, **kw):
        k = (svc, json.dumps(kw, sort_keys=True))
        with self.lock:
            if k not in self._clients:
                if svc == "s3" and "signature_version" not in kw:
                    kw["signature_version"] = "s3v4"  # what ~/.aws/emulator/<port>/config sets
                if kw.get("signature_version") == "botocore-default":
                    kw.pop("signature_version")
                cfg = Config(inject_host_prefix=False, retries={"max_attempts": 1, "mode": "standard"},
                             connect_timeout=5, read_timeout=kw.pop("read_timeout", 30),
                             proxies={}, **kw)
                self._clients[k] = self.session.client(svc, endpoint_url=self.endpoint,
                                                       verify=self.cfg["verify"], config=cfg)
            return self._clients[k]

    def http(self, url, method="GET", data=None, headers=None, timeout=15):
        ctx = None
        if url.startswith("https"):
            ctx = ssl.create_default_context(cafile=self.cfg["verify"])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             urllib.request.HTTPSHandler(context=ctx))
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


# ------------------------------------------------------------- outcomes ----
class Wrong(Exception):
    """Call succeeded but semantics differ from AWS."""


class Skip(Exception):
    pass


def classify(e):
    if isinstance(e, Wrong):
        return "WRONG_SEMANTICS", str(e)
    if isinstance(e, Skip):
        return "SKIP", str(e)
    if isinstance(e, ClientError):
        md = e.response.get("ResponseMetadata", {})
        st = md.get("HTTPStatusCode")
        err = e.response.get("Error", {})
        txt = f"HTTP {st} {err.get('Code')}: {err.get('Message')} (op={e.operation_name})"
        if st == 501 or err.get("Code") in ("NotImplemented", "NotImplementedException", "UnknownOperationException"):
            return "NOT_IMPLEMENTED", txt
        return "FAIL", txt
    return "FAIL", f"{type(e).__name__}: {e}"


def err_text(e):
    if isinstance(e, ClientError):
        er = e.response.get("Error", {})
        return {"http": e.response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
                "code": er.get("Code"), "message": er.get("Message")}
    return {"exception": f"{type(e).__name__}: {e}"}


def expect_error(fn, code):
    try:
        fn()
    except ClientError as e:
        got = e.response["Error"].get("Code")
        if got != code:
            raise Wrong(f"expected {code}, got {got}: {e.response['Error'].get('Message')}")
        return err_text(e)
    raise Wrong(f"expected {code}, call succeeded")


def n(prefix):
    return f"m50-{prefix}-{RUN}-{uuid.uuid4().hex[:4]}"


def poll(fn, timeout=10, interval=0.5):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    return last


def drain(t, url, want=1, timeout=10, wait=1):
    msgs = []
    end = time.time() + timeout
    sqs = t.c("sqs")
    while time.time() < end and len(msgs) < want:
        r = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=wait,
                                MessageAttributeNames=["All"], AttributeNames=["All"])
        for m in r.get("Messages", []):
            msgs.append(m)
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    return msgs


def queue(t, name=None, attrs=None):
    sqs = t.c("sqs")
    url = sqs.create_queue(QueueName=name or n("q"), Attributes=attrs or {})["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    return url, arn


# ------------------------------------------------------------ lambda zip ---
LAMBDA_SRC = r'''
import json, os, sys, time
def handler(event, context):
    d = os.environ.get("MARKER_DIR")
    wrote = None
    if d:
        try:
            os.makedirs(d, exist_ok=True)
            wrote = os.path.join(d, "%s-%d.json" % (context.function_name, time.time_ns()))
            with open(wrote, "w") as f:
                json.dump(event, f)
        except Exception as e:
            print("MARKER_FAILED", repr(e))
            wrote = None
    print("MARK50_EVENT " + json.dumps(event)[:1500])
    sq = os.environ.get("SIGNAL_QUEUE")
    if sq:
        try:
            import boto3
            boto3.client("sqs").send_message(QueueUrl=sq, MessageBody=json.dumps({"fn": context.function_name, "event": event})[:200000])
        except Exception as e:
            print("SIGNAL_FAILED", repr(e))
    if isinstance(event, dict) and event.get("fail"):
        raise ValueError("boom requested by probe")
    return {"ok": True, "my_var": os.environ.get("MY_VAR"), "fn": context.function_name,
            "python": sys.version.split()[0], "marker": wrote,
            "aws_region_env": os.environ.get("AWS_REGION"),
            "endpoint_env": os.environ.get("AWS_ENDPOINT_URL")}
'''


def lambda_zip():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("app.py", LAMBDA_SRC)
    return b.getvalue()


TRUST = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {
    "Service": ["lambda.amazonaws.com", "states.amazonaws.com", "scheduler.amazonaws.com", "events.amazonaws.com"]},
    "Action": "sts:AssumeRole"}]})
ALLOW_ALL = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]})


def ensure_role(t):
    with t.lock:
        if "role_arn" in t.shared:
            return t.shared["role_arn"]
    iam = t.c("iam")
    rn = n("role")
    arn = iam.create_role(RoleName=rn, AssumeRolePolicyDocument=TRUST)["Role"]["Arn"]
    iam.put_role_policy(RoleName=rn, PolicyName="dev-allow-all", PolicyDocument=ALLOW_ALL)
    t.shared["role_arn"] = arn
    return arn


def bare_role(t):
    """Role with trust policy but NO permission policies (least-privilege test)."""
    rn = n("bare")
    return t.c("iam").create_role(RoleName=rn, AssumeRolePolicyDocument=TRUST)["Role"]["Arn"]


def make_function(t, suffix="fn", marker_dir=None, signal_url=None, role=None, src=None):
    lam = t.c("lambda")
    fn = n(suffix)
    env = {"MY_VAR": "hello-" + t.name}
    if marker_dir:
        env["MARKER_DIR"] = marker_dir
    if signal_url:
        env["SIGNAL_QUEUE"] = signal_url
    code = lambda_zip() if src is None else zip_src(src)
    r = lam.create_function(FunctionName=fn, Runtime="python3.12", Role=role or ensure_role(t), Handler="app.handler",
                            Code={"ZipFile": code}, Timeout=30, Environment={"Variables": env})
    # wait until Active
    def active():
        s = lam.get_function_configuration(FunctionName=fn)
        return s.get("State") in (None, "Active") and s.get("LastUpdateStatus") in (None, "Successful")
    poll(active, timeout=30)
    return fn, r["FunctionArn"]


def zip_src(src):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("app.py", src)
    return b.getvalue()


def markers(d):
    try:
        return [os.path.join(d, f) for f in os.listdir(d)]
    except FileNotFoundError:
        return []


def lambda_logs(t, fn, needle="MARK50_EVENT"):
    try:
        r = t.c("logs").filter_log_events(logGroupName=f"/aws/lambda/{fn}", filterPattern=f'"{needle}"')
        return [e["message"] for e in r.get("events", [])]
    except ClientError as e:
        return [f"<logs error {err_text(e)}>"]


def fired(t, fn, mdir, timeout=15, signal_url=None):
    """Evidence a function ran: signal message sent by the function via in-Lambda boto3, marker file
    (host /tmp, only if the runtime shares it) or CloudWatch Logs."""
    def chk():
        if signal_url:
            got = drain(t, signal_url, want=1, timeout=1)
            if got:
                return {"via": "signal_queue (boto3 inside the function)", "count": len(got),
                        "event_sample": got[0]["Body"][:160]}
        m = markers(mdir)
        if m:
            return {"via": "marker_file", "count": len(m)}
        lg = [x for x in lambda_logs(t, fn) if not x.startswith("<logs error")]
        if lg:
            return {"via": "cloudwatch_logs", "count": len(lg), "sample": lg[0][:200]}
        return None
    return poll(chk, timeout=timeout, interval=1)


# ================================================================ checks ===
CHECKS = []


def check(cid, timeout=60):
    def deco(f):
        CHECKS.append((cid, f, timeout))
        return f
    return deco


# ---------------------------------------------------------------- S3 -------
@check("s3.basic_put_get")
def _(t):
    s3 = t.c("s3")
    b = n("b")
    s3.create_bucket(Bucket=b)
    s3.put_object(Bucket=b, Key="a/b.txt", Body=b"hello", ContentType="text/plain")
    o = s3.get_object(Bucket=b, Key="a/b.txt")
    body = o["Body"].read()
    if body != b"hello":
        raise Wrong(f"body {body!r}")
    try:
        s3.get_object(Bucket=b, Key="missing")
        raise Wrong("missing key did not raise")
    except ClientError as e:
        miss = err_text(e)
    t.shared["bucket"] = b
    return {"etag": o["ETag"], "ctype": o.get("ContentType"), "missing_key_error": miss}


@check("s3.multipart")
def _(t):
    s3 = t.c("s3")
    b = n("mp")
    s3.create_bucket(Bucket=b)
    # AWS rejects non-final parts < 5 MiB with EntityTooSmall: test that first
    up = s3.create_multipart_upload(Bucket=b, Key="small")["UploadId"]
    p1 = s3.upload_part(Bucket=b, Key="small", UploadId=up, PartNumber=1, Body=b"x" * 1024)
    p2 = s3.upload_part(Bucket=b, Key="small", UploadId=up, PartNumber=2, Body=b"y" * 10)
    small = None
    try:
        s3.complete_multipart_upload(Bucket=b, Key="small", UploadId=up, MultipartUpload={"Parts": [
            {"PartNumber": 1, "ETag": p1["ETag"]}, {"PartNumber": 2, "ETag": p2["ETag"]}]})
        small = "accepted 1 KiB non-final part (AWS: EntityTooSmall)"
    except ClientError as e:
        small = err_text(e)
    part = b"a" * (5 * 1024 * 1024)
    up = s3.create_multipart_upload(Bucket=b, Key="big")["UploadId"]
    e1 = s3.upload_part(Bucket=b, Key="big", UploadId=up, PartNumber=1, Body=part)["ETag"]
    e2 = s3.upload_part(Bucket=b, Key="big", UploadId=up, PartNumber=2, Body=b"tail")["ETag"]
    r = s3.complete_multipart_upload(Bucket=b, Key="big", UploadId=up, MultipartUpload={"Parts": [
        {"PartNumber": 1, "ETag": e1}, {"PartNumber": 2, "ETag": e2}]})
    h = s3.head_object(Bucket=b, Key="big")
    ev = {"etag": r["ETag"], "size": h["ContentLength"], "small_part_behaviour": small}
    if h["ContentLength"] != len(part) + 4:
        raise Wrong(f"size {h['ContentLength']}")
    if not r["ETag"].strip('"').endswith("-2"):
        raise Wrong(f"multipart ETag lacks -2 suffix: {r['ETag']} | {ev}")
    if isinstance(small, str):
        raise Wrong(small + f" | {ev}")
    return ev


@check("s3.presigned_url")
def _(t):
    s3 = t.c("s3")
    b = n("ps")
    s3.create_bucket(Bucket=b)
    put_url = s3.generate_presigned_url("put_object", Params={"Bucket": b, "Key": "p.txt"}, ExpiresIn=60)
    st, body = t.http(put_url, method="PUT", data=b"presigned-body")
    if st != 200:
        raise Wrong(f"presigned PUT -> {st} {body[:300]!r}")
    url = s3.generate_presigned_url("get_object", Params={"Bucket": b, "Key": "p.txt"}, ExpiresIn=60)
    st, body = t.http(url)
    if st != 200 or body != b"presigned-body":
        raise Wrong(f"presigned GET -> {st} {body[:300]!r}")
    # tampered signature must be rejected
    bad = url.replace("Signature=", "Signature=00")
    st2, body2 = t.http(bad)
    ev = {"url_host": url.split("/")[2], "tampered_status": st2, "tampered_body": body2[:200].decode(errors="replace")}
    exp = s3.generate_presigned_url("get_object", Params={"Bucket": b, "Key": "p.txt"}, ExpiresIn=1)
    time.sleep(2.2)
    st3, body3 = t.http(exp)
    ev["expired_status"] = st3
    ev["expired_body"] = body3[:200].decode(errors="replace")
    if st2 == 200:
        raise Wrong(f"tampered signature accepted | {ev}")
    if st3 == 200:
        raise Wrong(f"expired URL accepted | {ev}")
    return ev


@check("s3.versioning")
def _(t):
    s3 = t.c("s3")
    b = n("ver")
    s3.create_bucket(Bucket=b)
    s3.put_bucket_versioning(Bucket=b, VersioningConfiguration={"Status": "Enabled"})
    v1 = s3.put_object(Bucket=b, Key="k", Body=b"one").get("VersionId")
    v2 = s3.put_object(Bucket=b, Key="k", Body=b"two").get("VersionId")
    if not v1 or v1 == v2:
        raise Wrong(f"version ids {v1} {v2}")
    old = s3.get_object(Bucket=b, Key="k", VersionId=v1)["Body"].read()
    d = s3.delete_object(Bucket=b, Key="k")
    vs = s3.list_object_versions(Bucket=b)
    ev = {"versions": len(vs.get("Versions", [])), "delete_markers": len(vs.get("DeleteMarkers", [])),
          "delete_marker_flag": d.get("DeleteMarker"), "status": s3.get_bucket_versioning(Bucket=b).get("Status")}
    if old != b"one":
        raise Wrong(f"old version body {old!r}")
    if ev["versions"] != 2 or ev["delete_markers"] != 1:
        raise Wrong(str(ev))
    try:
        s3.get_object(Bucket=b, Key="k")
        raise Wrong("GET after delete marker succeeded")
    except ClientError as e:
        ev["get_after_delete"] = err_text(e)
    return ev


@check("s3.notify_sqs", timeout=45)
def _(t):
    s3 = t.c("s3")
    b = n("ns")
    s3.create_bucket(Bucket=b)
    url, arn = queue(t)
    try:
        s3.put_bucket_notification_configuration(Bucket=b, NotificationConfiguration={"QueueConfigurations": [
            {"QueueArn": arn, "Events": ["s3:ObjectCreated:*"]}]})
        no_pol = "ACCEPTED without queue policy (AWS: InvalidArgument Unable to validate destination)"
    except ClientError as e:
        no_pol = err_text(e)
    t.c("sqs").set_queue_attributes(QueueUrl=url, Attributes={"Policy": json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "s3.amazonaws.com"},
                                                "Action": "sqs:SendMessage", "Resource": arn}]})})
    s3.put_bucket_notification_configuration(Bucket=b, NotificationConfiguration={"QueueConfigurations": [
        {"QueueArn": arn, "Events": ["s3:ObjectCreated:*"]}]})
    s3.put_object(Bucket=b, Key="up/file.txt", Body=b"x")
    msgs = drain(t, url, want=2, timeout=8)
    bodies = [json.loads(m["Body"]) for m in msgs]
    test_event = any(x.get("Event") == "s3:TestEvent" for x in bodies)
    recs = [r for x in bodies for r in x.get("Records", [])]
    ev = {"messages": len(msgs), "s3_TestEvent_seen": test_event, "config_without_queue_policy": no_pol,
          "event_names": [r.get("eventName") for r in recs],
          "key": [r.get("s3", {}).get("object", {}).get("key") for r in recs]}
    if not recs:
        raise Wrong(f"no ObjectCreated record delivered in 8s | {ev}")
    return ev


@check("s3.notify_lambda", timeout=60)
def _(t):
    s3 = t.c("s3")
    mdir = f"{MARKER_ROOT}/{t.name}/s3lambda"
    sig, _ = queue(t)
    fn, farn = make_function(t, "s3fn", marker_dir=mdir, signal_url=sig)
    b = n("nl")
    s3.create_bucket(Bucket=b)
    # AWS validates the destination: without a lambda resource policy for s3 this is rejected
    try:
        s3.put_bucket_notification_configuration(Bucket=b, NotificationConfiguration={
            "LambdaFunctionConfigurations": [{"LambdaFunctionArn": farn, "Events": ["s3:ObjectCreated:*"]}]})
        no_perm = "ACCEPTED without lambda:AddPermission (AWS: InvalidArgument Unable to validate destination)"
    except ClientError as e:
        no_perm = err_text(e)
    t.c("lambda").add_permission(FunctionName=fn, StatementId="s3", Action="lambda:InvokeFunction",
                                 Principal="s3.amazonaws.com")
    s3.put_bucket_notification_configuration(Bucket=b, NotificationConfiguration={
        "LambdaFunctionConfigurations": [{"LambdaFunctionArn": farn, "Events": ["s3:ObjectCreated:*"]}]})
    s3.put_object(Bucket=b, Key="trigger.txt", Body=b"x")
    f = fired(t, fn, mdir, timeout=20, signal_url=sig)
    ev = {"fired": f, "config_without_permission": no_perm}
    if not f:
        raise Wrong(f"config accepted, but no evidence the Lambda ran within 20s | {ev}")
    return ev


# ---------------------------------------------------------- DynamoDB -------
def ddb_table(t, gsi=False, stream=False):
    d = t.c("dynamodb")
    tn = n("t")
    kw = dict(TableName=tn, BillingMode="PAY_PER_REQUEST",
              AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                    {"AttributeName": "sk", "AttributeType": "S"}],
              KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
    if gsi:
        kw["AttributeDefinitions"].append({"AttributeName": "g", "AttributeType": "S"})
        kw["GlobalSecondaryIndexes"] = [{"IndexName": "byG", "KeySchema": [{"AttributeName": "g", "KeyType": "HASH"}],
                                         "Projection": {"ProjectionType": "ALL"}}]
    if stream:
        kw["StreamSpecification"] = {"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"}
    r = d.create_table(**kw)
    poll(lambda: d.describe_table(TableName=tn)["Table"]["TableStatus"] == "ACTIVE", timeout=20)
    return tn, r


@check("ddb.conditional_put_fails")
def _(t):
    d = t.c("dynamodb")
    tn, _ = ddb_table(t)
    item = {"pk": {"S": "a"}, "sk": {"S": "1"}}
    d.put_item(TableName=tn, Item=item)
    e = expect_error(lambda: d.put_item(TableName=tn, Item=item, ConditionExpression="attribute_not_exists(pk)"),
                     "ConditionalCheckFailedException")
    # ReturnValuesOnConditionCheckFailure returns the existing item on AWS
    try:
        d.put_item(TableName=tn, Item=item, ConditionExpression="attribute_not_exists(pk)",
                   ReturnValuesOnConditionCheckFailure="ALL_OLD")
    except ClientError as ex:
        e["ALL_OLD_item_returned"] = "Item" in ex.response
    return e


@check("ddb.transact_cancel_reasons")
def _(t):
    d = t.c("dynamodb")
    tn, _ = ddb_table(t)
    d.put_item(TableName=tn, Item={"pk": {"S": "x"}, "sk": {"S": "1"}})
    try:
        d.transact_write_items(TransactItems=[
            {"Put": {"TableName": tn, "Item": {"pk": {"S": "y"}, "sk": {"S": "1"}}}},
            {"Put": {"TableName": tn, "Item": {"pk": {"S": "x"}, "sk": {"S": "1"}},
                     "ConditionExpression": "attribute_not_exists(pk)"}}])
        raise Wrong("transaction succeeded")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        reasons = e.response.get("CancellationReasons")
        ev = {**err_text(e), "CancellationReasons": reasons}
        if code != "TransactionCanceledException":
            raise Wrong(f"code {code} | {ev}")
        y = d.get_item(TableName=tn, Key={"pk": {"S": "y"}, "sk": {"S": "1"}}).get("Item")
        ev["first_put_rolled_back"] = y is None
        if y is not None:
            raise Wrong(f"partial write leaked | {ev}")
        if not reasons or [r.get("Code") for r in reasons] != ["None", "ConditionalCheckFailed"]:
            raise Wrong(f"CancellationReasons not AWS-shaped | {ev}")
        return ev


@check("ddb.gsi_query")
def _(t):
    d = t.c("dynamodb")
    tn, _ = ddb_table(t, gsi=True)
    for i in range(3):
        d.put_item(TableName=tn, Item={"pk": {"S": f"p{i}"}, "sk": {"S": "s"}, "g": {"S": "red" if i < 2 else "blue"}})
    r = d.query(TableName=tn, IndexName="byG", KeyConditionExpression="g = :g",
                ExpressionAttributeValues={":g": {"S": "red"}})
    if r["Count"] != 2:
        raise Wrong(f"GSI count {r['Count']}")
    try:
        d.query(TableName=tn, IndexName="byG", KeyConditionExpression="g = :g",
                ExpressionAttributeValues={":g": {"S": "red"}}, ConsistentRead=True)
        cr = "ConsistentRead on GSI accepted (AWS: ValidationException)"
    except ClientError as e:
        cr = err_text(e)
    if isinstance(cr, str):
        raise Wrong(cr)
    return {"count": r["Count"], "consistent_read_on_gsi": cr}


@check("ddb.streams")
def _(t):
    d = t.c("dynamodb")
    tn, r = ddb_table(t, stream=True)
    sarn = d.describe_table(TableName=tn)["Table"].get("LatestStreamArn")
    if not sarn:
        raise Wrong("no LatestStreamArn")
    d.put_item(TableName=tn, Item={"pk": {"S": "s"}, "sk": {"S": "1"}, "v": {"N": "1"}})
    d.update_item(TableName=tn, Key={"pk": {"S": "s"}, "sk": {"S": "1"}}, UpdateExpression="SET v = :v",
                  ExpressionAttributeValues={":v": {"N": "2"}})
    ds = t.c("dynamodbstreams")
    def get():
        out = []
        for sh in ds.describe_stream(StreamArn=sarn)["StreamDescription"]["Shards"]:
            it = ds.get_shard_iterator(StreamArn=sarn, ShardId=sh["ShardId"], ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
            out += ds.get_records(ShardIterator=it)["Records"]
        return out if len(out) >= 2 else None
    recs = poll(get, timeout=10) or []
    ev = {"records": len(recs), "events": [x["eventName"] for x in recs],
          "has_old_image_on_modify": any("OldImage" in x["dynamodb"] for x in recs if x["eventName"] == "MODIFY")}
    if ev["events"][:2] != ["INSERT", "MODIFY"] or not ev["has_old_image_on_modify"]:
        raise Wrong(str(ev))
    return ev


@check("ddb.ttl")
def _(t):
    d = t.c("dynamodb")
    tn, _ = ddb_table(t)
    d.update_time_to_live(TableName=tn, TimeToLiveSpecification={"Enabled": True, "AttributeName": "exp"})
    st = d.describe_time_to_live(TableName=tn)["TimeToLiveDescription"]
    past = int(time.time()) - 3600
    d.put_item(TableName=tn, Item={"pk": {"S": "t"}, "sk": {"S": "1"}, "exp": {"N": str(past)}})
    time.sleep(3)
    still = "Item" in d.get_item(TableName=tn, Key={"pk": {"S": "t"}, "sk": {"S": "1"}})
    if st.get("TimeToLiveStatus") not in ("ENABLED", "ENABLING") or st.get("AttributeName") != "exp":
        raise Wrong(str(st))
    return {"describe": st, "expired_item_still_readable_after_3s": still,
            "note": "AWS deletes expired items lazily (up to days), so 'still readable' is AWS-consistent"}


@check("ddb.pagination")
def _(t):
    d = t.c("dynamodb")
    tn, _ = ddb_table(t)
    for i in range(25):
        d.put_item(TableName=tn, Item={"pk": {"S": "p"}, "sk": {"S": f"{i:03d}"}})
    pages, total, lek = 0, 0, None
    while True:
        kw = dict(TableName=tn, KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": {"S": "p"}}, Limit=10)
        if lek:
            kw["ExclusiveStartKey"] = lek
        r = d.query(**kw)
        pages += 1
        total += r["Count"]
        lek = r.get("LastEvaluatedKey")
        if not lek or pages > 10:
            break
    ev = {"pages": pages, "total": total}
    # AWS: 25 items/Limit 10 -> pages of 10,10,5 (3 pages, last without LEK)
    if total != 25 or pages != 3:
        raise Wrong(str(ev))
    return ev


@check("ddb.error_texts")
def _(t):
    """Not pass/fail on its own: records exact error strings for side-by-side comparison."""
    d = t.c("dynamodb")
    tn, _ = ddb_table(t)
    out = {}
    for label, fn in {
        "missing_table": lambda: d.get_item(TableName="nope-" + RUN, Key={"pk": {"S": "a"}, "sk": {"S": "b"}}),
        "bad_key_type": lambda: d.get_item(TableName=tn, Key={"pk": {"N": "1"}, "sk": {"S": "b"}}),
        "missing_sk": lambda: d.put_item(TableName=tn, Item={"pk": {"S": "a"}}),
        "bad_expression": lambda: d.update_item(TableName=tn, Key={"pk": {"S": "a"}, "sk": {"S": "b"}},
                                                UpdateExpression="SET = 1"),
        "undefined_placeholder": lambda: d.query(TableName=tn, KeyConditionExpression="pk = :nope"),
    }.items():
        try:
            fn()
            out[label] = "NO ERROR"
        except ClientError as e:
            out[label] = err_text(e)
    bad = [k for k, v in out.items() if v == "NO ERROR"]
    if bad:
        raise Wrong(f"accepted invalid input: {bad} | {out}")
    return out


# ---------------------------------------------------------------- SQS ------
@check("sqs.standard")
def _(t):
    url, _ = queue(t)
    sqs = t.c("sqs")
    sqs.send_message(QueueUrl=url, MessageBody="hi", MessageAttributes={"a": {"DataType": "String", "StringValue": "b"}})
    m = drain(t, url, want=1, timeout=5)
    if not m or m[0]["Body"] != "hi" or m[0].get("MessageAttributes", {}).get("a", {}).get("StringValue") != "b":
        raise Wrong(str(m))
    return {"md5_body": m[0]["MD5OfBody"], "attrs": list(m[0].get("Attributes", {}).keys())}


@check("sqs.fifo_dedup")
def _(t):
    sqs = t.c("sqs")
    url, _ = queue(t, n("f") + ".fifo", {"FifoQueue": "true"})
    for _i in range(2):
        sqs.send_message(QueueUrl=url, MessageBody="dup", MessageGroupId="g", MessageDeduplicationId="d1")
    sqs.send_message(QueueUrl=url, MessageBody="second", MessageGroupId="g", MessageDeduplicationId="d2")
    msgs = drain(t, url, want=3, timeout=4)
    bodies = [m["Body"] for m in msgs]
    missing = expect_error(lambda: sqs.send_message(QueueUrl=url, MessageBody="x", MessageGroupId="g"),
                           "InvalidParameterValue")
    ev = {"bodies": bodies, "no_dedup_id_error": missing}
    if bodies != ["dup", "second"]:
        raise Wrong(str(ev))
    return ev


@check("sqs.dlq_redrive", timeout=45)
def _(t):
    sqs = t.c("sqs")
    durl, darn = queue(t)
    url, _ = queue(t, attrs={"VisibilityTimeout": "1",
                             "RedrivePolicy": json.dumps({"deadLetterTargetArn": darn, "maxReceiveCount": "2"})})
    sqs.send_message(QueueUrl=url, MessageBody="poison")
    receives = 0
    for _i in range(6):
        r = sqs.receive_message(QueueUrl=url, WaitTimeSeconds=1, AttributeNames=["ApproximateReceiveCount"])
        if r.get("Messages"):
            receives += 1
        time.sleep(1.2)
    dl = drain(t, durl, want=1, timeout=5)
    ev = {"receives_from_source": receives, "in_dlq": len(dl)}
    if not dl or receives != 2:
        raise Wrong(str(ev))
    # StartMessageMoveTask (redrive back)
    try:
        sqs.send_message(QueueUrl=durl, MessageBody="back")
        tid = sqs.start_message_move_task(SourceArn=darn)["TaskHandle"]
        ev["start_message_move_task"] = "ok"
    except ClientError as e:
        ev["start_message_move_task"] = err_text(e)
    return ev


@check("sqs.long_poll")
def _(t):
    sqs = t.c("sqs")
    url, _ = queue(t)
    t0 = time.time()
    sqs.receive_message(QueueUrl=url, WaitTimeSeconds=3)
    empty_wait = round(time.time() - t0, 2)
    def later():
        time.sleep(1)
        t.c("sqs").send_message(QueueUrl=url, MessageBody="late")
    threading.Thread(target=later, daemon=True).start()
    t0 = time.time()
    r = sqs.receive_message(QueueUrl=url, WaitTimeSeconds=5)
    wake = round(time.time() - t0, 2)
    ev = {"empty_wait_s": empty_wait, "woke_after_s": wake, "got": len(r.get("Messages", []))}
    if empty_wait < 2.5 or not r.get("Messages") or wake > 4:
        raise Wrong(str(ev))
    return ev


# ---------------------------------------------------------------- SNS ------
def allow_service(t, url, arn, service, source_arn):
    t.c("sqs").set_queue_attributes(QueueUrl=url, Attributes={"Policy": json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
                                                "Action": "sqs:SendMessage", "Resource": arn,
                                                "Condition": {"ArnEquals": {"aws:SourceArn": source_arn}}}]})})


@check("sns.sqs_filter_policy", timeout=60)
def _(t):
    sns = t.c("sns")
    top = sns.create_topic(Name=n("top"))["TopicArn"]
    # phase 0: queue WITHOUT a queue policy (AWS silently drops: SNS is not allowed to SendMessage)
    u0, a0 = queue(t)
    sns.subscribe(TopicArn=top, Protocol="sqs", Endpoint=a0)
    sns.publish(TopicArn=top, Message="no-policy")
    no_pol = len(drain(t, u0, want=1, timeout=4))
    u1, a1 = queue(t)
    u2, a2 = queue(t)
    for u, a in ((u1, a1), (u2, a2)):
        allow_service(t, u, a, "sns.amazonaws.com", top)
    sns.subscribe(TopicArn=top, Protocol="sqs", Endpoint=a1,
                  Attributes={"FilterPolicy": json.dumps({"color": ["red"]}), "RawMessageDelivery": "true"})
    sns.subscribe(TopicArn=top, Protocol="sqs", Endpoint=a2)
    for c in ("red", "blue"):
        sns.publish(TopicArn=top, Message=f"m-{c}", MessageAttributes={"color": {"DataType": "String", "StringValue": c}})
    m1 = drain(t, u1, want=2, timeout=5)
    m2 = drain(t, u2, want=2, timeout=5)
    ev = {"delivered_without_queue_policy": no_pol,
          "filtered_queue": [m["Body"] for m in m1],
          "unfiltered_queue_count": len(m2),
          "unfiltered_envelope_type": json.loads(m2[0]["Body"]).get("Type") if m2 else None}
    if ev["filtered_queue"] != ["m-red"] or len(m2) != 2 or ev["unfiltered_envelope_type"] != "Notification":
        raise Wrong(str(ev))
    return ev


# ------------------------------------------------------------- Lambda ------
@check("lambda.invoke_env", timeout=60)
def _(t):
    lam = t.c("lambda", read_timeout=60)
    mdir = f"{MARKER_ROOT}/{t.name}/invoke"
    fn, arn = make_function(t, "inv", marker_dir=mdir)
    t0 = time.time()
    r = lam.invoke(FunctionName=fn, Payload=json.dumps({"hello": "world"}))
    dur = round(time.time() - t0, 2)
    payload = json.loads(r["Payload"].read() or b"null")
    t0 = time.time()
    lam.invoke(FunctionName=fn, Payload=b"{}")
    warm = round(time.time() - t0, 2)
    ev = {"status": r["StatusCode"], "payload": payload, "cold_s": dur, "warm_s": warm,
          "marker_on_host": bool(markers(mdir)), "FunctionError": r.get("FunctionError")}
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise Wrong(f"handler did not run / wrong payload | {ev}")
    if payload.get("my_var") != "hello-" + t.name:
        raise Wrong(f"env var not visible | {ev}")
    t.shared["fn"] = (fn, arn)
    return ev


@check("lambda.error_shape", timeout=60)
def _(t):
    lam = t.c("lambda", read_timeout=60)
    fn, _a = make_function(t, "err")
    r = lam.invoke(FunctionName=fn, Payload=json.dumps({"fail": True}))
    p = json.loads(r["Payload"].read() or b"null")
    ev = {"status": r["StatusCode"], "FunctionError": r.get("FunctionError"), "payload": p}
    if r.get("FunctionError") != "Unhandled" or not isinstance(p, dict) or p.get("errorType") != "ValueError":
        raise Wrong(str(ev))
    # async invoke shape
    ra = lam.invoke(FunctionName=fn, InvocationType="Event", Payload=b"{}")
    ev["event_invoke_status"] = ra["StatusCode"]
    rd = lam.invoke(FunctionName=fn, InvocationType="DryRun", Payload=b"{}")
    ev["dryrun_status"] = rd["StatusCode"]
    if ra["StatusCode"] != 202 or rd["StatusCode"] != 204:
        raise Wrong(str(ev))
    return ev


@check("lambda.esm_sqs", timeout=75)
def _(t):
    lam = t.c("lambda")
    mdir = f"{MARKER_ROOT}/{t.name}/esm"
    sig, _ = queue(t)
    fn, arn = make_function(t, "esm", marker_dir=mdir, signal_url=sig)
    url, qarn = queue(t, attrs={"VisibilityTimeout": "30"})
    m = lam.create_event_source_mapping(EventSourceArn=qarn, FunctionName=fn, BatchSize=1, Enabled=True)
    poll(lambda: lam.get_event_source_mapping(UUID=m["UUID"])["State"] == "Enabled", timeout=20)
    t.c("sqs").send_message(QueueUrl=url, MessageBody="esm-ping")
    f = fired(t, fn, mdir, timeout=30, signal_url=sig)
    time.sleep(1)
    left = t.c("sqs").get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    ev = {"fired": f, "esm_state": lam.get_event_source_mapping(UUID=m["UUID"])["State"],
          "queue_visible": left.get("ApproximateNumberOfMessages"),
          "queue_inflight": left.get("ApproximateNumberOfMessagesNotVisible")}
    if not f:
        raise Wrong(f"mapping created but function never ran within 30s | {ev}")
    return ev


RUNTIME_SRC = r'''
import json, os, sys, socket
def handler(event, context):
    out = {"python": sys.version.split()[0], "impl": sys.version.split("[")[-1][:40],
           "canary_env": os.environ.get("M50_CANARY"), "env_count": len(os.environ),
           "endpoint_env": os.environ.get("AWS_ENDPOINT_URL")}
    try:
        import requests  # NOT in the deployment zip and NOT in the AWS python3.12 runtime
        out["undeclared_import_requests"] = "importable"
    except Exception as e:
        out["undeclared_import_requests"] = type(e).__name__
    try:
        with open("/etc/hostname") as f:
            out["host_fs_read"] = "ok"
    except Exception as e:
        out["host_fs_read"] = repr(e)[:100]
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=2).close()
        out["internet"] = "ok"
    except Exception as e:
        out["internet"] = repr(e)[:80]
    return out
'''


@check("lambda.runtime_fidelity", timeout=60)
def _(t):
    """What the handler actually runs on: interpreter, isolation, env leakage, undeclared deps."""
    fn, _a = make_function(t, "rt", src=RUNTIME_SRC)
    p = json.loads(t.c("lambda", read_timeout=60).invoke(FunctionName=fn, Payload=b"{}")["Payload"].read())
    probs = []
    if not str(p.get("python", "")).startswith("3.12"):
        probs.append(f"requested python3.12, ran {p.get('python')}")
    if p.get("canary_env"):
        probs.append("emulator process env var M50_CANARY leaked into the function")
    if p.get("undeclared_import_requests") == "importable":
        probs.append("'requests' importable without packaging it (AWS: ImportModuleError)")
    if probs:
        raise Wrong("; ".join(probs) + f" | {p}")
    return p


@check("lambda.logs", timeout=60)
def _(t):
    lam = t.c("lambda", read_timeout=60)
    fn, _a = make_function(t, "log")
    r = lam.invoke(FunctionName=fn, Payload=json.dumps({"x": 1}), LogType="Tail")
    tail = base64.b64decode(r.get("LogResult") or b"").decode(errors="replace")
    time.sleep(2)
    lg = lambda_logs(t, fn)
    ev = {"LogResult_tail_has_REPORT": "REPORT RequestId" in tail, "tail_sample": tail[:160],
          "cloudwatch_log_lines": len([x for x in lg if not x.startswith("<logs error")]),
          "cloudwatch_error": [x for x in lg if x.startswith("<logs error")][:1]}
    # same function code, execution role WITHOUT logs:* permissions (AWS writes no logs then)
    fn2, _b = make_function(t, "log2", role=bare_role(t))
    lam.invoke(FunctionName=fn2, Payload=json.dumps({"x": 2}))
    time.sleep(2)
    ev["cloudwatch_lines_when_role_lacks_logs_perms"] = len([x for x in lambda_logs(t, fn2) if not x.startswith("<logs error")])
    probs = []
    if not ev["LogResult_tail_has_REPORT"]:
        probs.append("LogType=Tail has handler output but no START/END/REPORT lines")
    if not ev["cloudwatch_log_lines"]:
        probs.append("nothing written to CloudWatch Logs /aws/lambda/<fn>")
    if probs:
        raise Wrong("; ".join(probs) + f" | {ev}")
    return ev


@check("lambda.iam_enforced_inside_function", timeout=60)
def _(t):
    """Execution role with NO permission policies calls S3 from inside the function."""
    src = '''
import boto3
def handler(e, c):
    try:
        return {"list_buckets": len(boto3.client("s3").list_buckets()["Buckets"])}
    except Exception as ex:
        return {"error": repr(ex)[:400]}
'''
    fn, _a = make_function(t, "iam", role=bare_role(t), src=src)
    p = json.loads(t.c("lambda", read_timeout=60).invoke(FunctionName=fn, Payload=b"{}")["Payload"].read())
    if "error" not in p:
        raise Wrong(f"role without policies could call s3:ListAllMyBuckets | {p}")
    if "AccessDenied" not in p["error"]:
        raise Wrong(f"failed but not with AccessDenied | {p}")
    return p


@check("s3.presigned_sigv2_default")
def _(t):
    """boto3 presign with NO signature_version configured (botocore default for presign is SigV2 query auth)."""
    s3 = t.c("s3", signature_version="botocore-default")
    b = n("v2")
    t.c("s3").create_bucket(Bucket=b)
    t.c("s3").put_object(Bucket=b, Key="k", Body=b"v2")
    u = s3.generate_presigned_url("get_object", Params={"Bucket": b, "Key": "k"}, ExpiresIn=60)
    st, body = t.http(u)
    ev = {"query_style": "SigV2 (AWSAccessKeyId=)" if "AWSAccessKeyId=" in u else "SigV4 (X-Amz-Credential=)",
          "status": st, "body": body[:200].decode(errors="replace")}
    if st != 200:
        raise Wrong(str(ev))
    return ev


@check("lambda.esm_ddb_stream", timeout=75)
def _(t):
    """DynamoDB Streams -> Lambda event source mapping (common for fan-out pipelines)."""
    lam = t.c("lambda")
    sig, _ = queue(t)
    fn, _a = make_function(t, "dsm", signal_url=sig)
    tn, _r = ddb_table(t, stream=True)
    sarn = t.c("dynamodb").describe_table(TableName=tn)["Table"]["LatestStreamArn"]
    m = lam.create_event_source_mapping(EventSourceArn=sarn, FunctionName=fn, StartingPosition="LATEST", BatchSize=1)
    poll(lambda: lam.get_event_source_mapping(UUID=m["UUID"])["State"] == "Enabled", timeout=20)
    time.sleep(1)
    t.c("dynamodb").put_item(TableName=tn, Item={"pk": {"S": "esm"}, "sk": {"S": "1"}})
    f = fired(t, fn, "/nonexistent", timeout=30, signal_url=sig)
    if not f:
        raise Wrong("mapping Enabled but function never ran within 30s")
    return f


@check("sfn.lambda_task", timeout=60)
def _(t):
    sf = t.c("stepfunctions")
    fn, farn = make_function(t, "sfl")
    d = {"StartAt": "Call", "States": {"Call": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
         "Parameters": {"FunctionName": farn, "Payload.$": "$"}, "OutputPath": "$.Payload", "End": True}}}
    arn = sf.create_state_machine(name=n("sml"), definition=json.dumps(d), roleArn=ensure_role(t))["stateMachineArn"]
    ex = sf.start_execution(stateMachineArn=arn, input=json.dumps({"k": "v"}))["executionArn"]
    r = poll(lambda: (lambda x: x if x["status"] != "RUNNING" else None)(sf.describe_execution(executionArn=ex)), timeout=30)
    ev = {"status": r and r["status"], "output": r and json.loads(r.get("output") or "null")}
    if not r or r["status"] != "SUCCEEDED" or not (ev["output"] or {}).get("ok"):
        raise Wrong(str(ev))
    return ev


@check("lambda.nodejs_invoke", timeout=60)
def _(t):
    lam = t.c("lambda", read_timeout=60)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("index.mjs", "export const handler = async (e) => ({ok: true, node: process.version, v: process.env.MY_VAR});")
    fn = n("node")
    lam.create_function(FunctionName=fn, Runtime="nodejs20.x", Role=ensure_role(t), Handler="index.handler",
                        Code={"ZipFile": b.getvalue()}, Environment={"Variables": {"MY_VAR": "n"}})
    poll(lambda: lam.get_function_configuration(FunctionName=fn).get("State") in (None, "Active"), timeout=20)
    r = lam.invoke(FunctionName=fn, Payload=b"{}")
    p = json.loads(r["Payload"].read() or b"null")
    ev = {"payload": p, "FunctionError": r.get("FunctionError")}
    if not isinstance(p, dict) or p.get("ok") is not True:
        raise Wrong(str(ev))
    if not str(p.get("node", "")).startswith("v20"):
        raise Wrong(f"requested nodejs20.x, ran {p.get('node')} | {ev}")
    return ev


# ------------------------------------------------------- EventBridge -------
@check("events.rule_to_sqs", timeout=45)
def _(t):
    eb = t.c("events")
    url, arn = queue(t)
    rn = n("rule")
    eb.put_rule(Name=rn, EventPattern=json.dumps({"source": ["mark50.test"], "detail-type": ["ping"],
                                                  "detail": {"n": [{"numeric": [">", 5]}]}}))
    rule_arn = eb.describe_rule(Name=rn)["Arn"]
    eb.put_targets(Rule=rn, Targets=[{"Id": "q", "Arn": arn}])
    eb.put_events(Entries=[{"Source": "mark50.test", "DetailType": "ping", "Detail": json.dumps({"n": 10})}])
    no_pol = len(drain(t, url, want=1, timeout=4))
    allow_service(t, url, arn, "events.amazonaws.com", rule_arn)
    r = eb.put_events(Entries=[
        {"Source": "mark50.test", "DetailType": "ping", "Detail": json.dumps({"n": 10})},
        {"Source": "mark50.test", "DetailType": "ping", "Detail": json.dumps({"n": 1})},
        {"Source": "other", "DetailType": "ping", "Detail": json.dumps({"n": 10})}])
    msgs = drain(t, url, want=3, timeout=6)
    ev = {"delivered_without_queue_policy": no_pol, "FailedEntryCount": r["FailedEntryCount"], "delivered": len(msgs),
          "detail": [json.loads(m["Body"]).get("detail") for m in msgs]}
    if len(msgs) != 1 or ev["detail"][0] != {"n": 10}:
        raise Wrong(str(ev))
    tp = eb.test_event_pattern(EventPattern=json.dumps({"source": ["a"]}), Event=json.dumps({
        "id": "1", "account": ACCOUNT, "source": "a", "time": "2026-01-01T00:00:00Z", "region": REGION,
        "resources": [], "detail-type": "x", "detail": {}}))
    ev["test_event_pattern"] = tp["Result"]
    return ev


@check("scheduler.one_shot_to_sqs", timeout=100)
def _(t):
    sch = t.c("scheduler")
    url, arn = queue(t)
    role = ensure_role(t)
    at = time.gmtime(time.time() + 3)
    expr = time.strftime("at(%Y-%m-%dT%H:%M:%S)", at)
    nm = n("sch")
    sch.create_schedule(Name=nm, ScheduleExpression=expr, FlexibleTimeWindow={"Mode": "OFF"},
                        Target={"Arn": arn, "RoleArn": role, "Input": json.dumps({"from": "scheduler"})})
    g = sch.get_schedule(Name=nm)
    msgs = drain(t, url, want=1, timeout=75, wait=2)
    ev = {"expression": expr, "state": g.get("State"), "delivered": len(msgs),
          "body": msgs[0]["Body"] if msgs else None}
    if not msgs:
        raise Wrong(f"schedule created but nothing delivered within 75s | {ev}")
    return ev


# ----------------------------------------------------- Step Functions ------
SFN_DEF = {
    "StartAt": "Init",
    "States": {
        "Init": {"Type": "Pass", "Result": {"n": 7}, "ResultPath": "$.data", "Next": "Decide"},
        "Decide": {"Type": "Choice", "Choices": [{"Variable": "$.data.n", "NumericGreaterThan": 5, "Next": "Pause"}],
                   "Default": "Small"},
        "Pause": {"Type": "Wait", "Seconds": 1, "Next": "Big"},
        "Big": {"Type": "Pass", "Result": "big", "ResultPath": "$.verdict", "End": True},
        "Small": {"Type": "Pass", "Result": "small", "ResultPath": "$.verdict", "End": True},
    }}


@check("sfn.execution", timeout=45)
def _(t):
    sf = t.c("stepfunctions")
    arn = sf.create_state_machine(name=n("sm"), definition=json.dumps(SFN_DEF), roleArn=ensure_role(t))["stateMachineArn"]
    ex = sf.start_execution(stateMachineArn=arn, input=json.dumps({"in": 1}))["executionArn"]
    t0 = time.time()
    d = poll(lambda: (lambda x: x if x["status"] != "RUNNING" else None)(sf.describe_execution(executionArn=ex)), timeout=20)
    took = round(time.time() - t0, 2)
    hist = sf.get_execution_history(executionArn=ex)["events"]
    ev = {"status": d and d["status"], "output": d and json.loads(d.get("output") or "null"), "seconds": took,
          "history_events": len(hist)}
    if not d or d["status"] != "SUCCEEDED" or ev["output"].get("verdict") != "big":
        raise Wrong(str(ev))
    # invalid definition must be rejected at create
    try:
        sf.create_state_machine(name=n("bad"), definition=json.dumps({"StartAt": "X", "States": {}}), roleArn=ensure_role(t))
        ev["invalid_definition"] = "ACCEPTED (AWS: InvalidDefinition)"
    except ClientError as e:
        ev["invalid_definition"] = err_text(e)
    if ev["invalid_definition"] == "ACCEPTED (AWS: InvalidDefinition)":
        raise Wrong(str(ev))
    return ev


@check("sfn.test_state")
def _(t):
    sf = t.c("stepfunctions")
    r = sf.test_state(definition=json.dumps({"Type": "Pass", "Parameters": {"x.$": "$.a"}, "End": True}),
                      roleArn=ensure_role(t), input=json.dumps({"a": 42}))
    ev = {"status": r.get("status"), "output": r.get("output")}
    if r.get("status") != "SUCCEEDED" or json.loads(r["output"]) != {"x": 42}:
        raise Wrong(str(ev))
    return ev


# ----------------------------------------------- KMS / Secrets / SSM -------
@check("kms.roundtrip")
def _(t):
    k = t.c("kms")
    kid = k.create_key(Description="m50")["KeyMetadata"]["KeyId"]
    c = k.encrypt(KeyId=kid, Plaintext=b"secret-bytes")["CiphertextBlob"]
    p = k.decrypt(CiphertextBlob=c)
    if p["Plaintext"] != b"secret-bytes":
        raise Wrong("roundtrip mismatch")
    if b"secret-bytes" in c:
        raise Wrong("plaintext visible inside ciphertext")
    k.disable_key(KeyId=kid)
    dis = expect_error(lambda: k.decrypt(CiphertextBlob=c), "DisabledException")
    return {"cipher_len": len(c), "decrypt_keyid_echo": p.get("KeyId"), "decrypt_disabled_key": dis}


@check("secrets.create_get_rotate", timeout=60)
def _(t):
    sm = t.c("secretsmanager")
    nm = n("sec")
    sm.create_secret(Name=nm, SecretString=json.dumps({"pw": "v1"}))
    v = sm.get_secret_value(SecretId=nm)
    ev = {"value_ok": json.loads(v["SecretString"]) == {"pw": "v1"}, "stages": v.get("VersionStages")}
    fn, farn = make_function(t, "rot")
    try:
        sm.rotate_secret(SecretId=nm, RotationLambdaARN=farn, RotationRules={"AutomaticallyAfterDays": 30},
                         RotateImmediately=False)
        ev["rotate_without_lambda_permission"] = "ACCEPTED"
    except ClientError as e:
        ev["rotate_without_lambda_permission"] = err_text(e)
    t.c("lambda").add_permission(FunctionName=fn, StatementId="sm", Action="lambda:InvokeFunction",
                                 Principal="secretsmanager.amazonaws.com")
    try:
        sm.rotate_secret(SecretId=nm, RotationLambdaARN=farn, RotationRules={"AutomaticallyAfterDays": 30},
                         RotateImmediately=False)
        dsc = sm.describe_secret(SecretId=nm)
        ev["rotation_enabled"] = dsc.get("RotationEnabled")
        ev["rotation_rules"] = dsc.get("RotationRules")
    except ClientError as e:
        ev["rotate_error"] = err_text(e)
        raise Wrong(str(ev))
    e = expect_error(lambda: sm.get_secret_value(SecretId="nope-" + RUN), "ResourceNotFoundException")
    ev["missing_secret_error"] = e
    if not ev["value_ok"] or ev.get("rotation_enabled") is not True:
        raise Wrong(str(ev))
    return ev


@check("ssm.securestring")
def _(t):
    ssm = t.c("ssm")
    nm = "/m50/" + RUN + "/db-password"
    ssm.put_parameter(Name=nm, Value="s3cr3t", Type="SecureString")
    plain = ssm.get_parameter(Name=nm, WithDecryption=True)["Parameter"]["Value"]
    raw = ssm.get_parameter(Name=nm, WithDecryption=False)["Parameter"]["Value"]
    byp = ssm.get_parameters_by_path(Path="/m50/" + RUN, WithDecryption=True)["Parameters"]
    ev = {"decrypted_ok": plain == "s3cr3t", "undecrypted_differs": raw != "s3cr3t", "raw_prefix": raw[:24],
          "by_path_count": len(byp)}
    if not ev["decrypted_ok"] or not ev["undecrypted_differs"] or len(byp) != 1:
        raise Wrong(str(ev))
    return ev


# ------------------------------------------------------------ STS / IAM ----
@check("sts.caller_identity")
def _(t):
    r = t.c("sts").get_caller_identity()
    return {"Account": r["Account"], "Arn": r["Arn"], "UserId": r["UserId"]}


@check("iam.deny_policy_enforced", timeout=45)
def _(t):
    iam = t.c("iam")
    rn = n("deny")
    acct = t.c("sts").get_caller_identity()["Account"]
    trust = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {
        "AWS": f"arn:aws:iam::{acct}:root"}, "Action": "sts:AssumeRole"}]})
    arn = iam.create_role(RoleName=rn, AssumeRolePolicyDocument=trust)["Role"]["Arn"]
    iam.put_role_policy(RoleName=rn, PolicyName="denyall", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Action": "*", "Resource": "*"}]}))
    pol = iam.create_policy(PolicyName=n("p"), PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}))
    iam.attach_role_policy(RoleName=rn, PolicyArn=pol["Policy"]["Arn"])
    try:
        cr = t.c("sts").assume_role(RoleArn=arn, RoleSessionName="m50")["Credentials"]
    except ClientError as e:
        return {"assume_role": err_text(e)}
    s = boto3.Session(aws_access_key_id=cr["AccessKeyId"], aws_secret_access_key=cr["SecretAccessKey"],
                      aws_session_token=cr["SessionToken"], region_name=REGION)
    s3 = s.client("s3", endpoint_url=t.endpoint, verify=t.cfg["verify"], config=Config(proxies={}, retries={"max_attempts": 1}))
    ev = {"assumed_key_prefix": cr["AccessKeyId"][:8]}
    try:
        s3.list_buckets()
        ev["list_buckets_with_deny_all"] = "ALLOWED"
    except ClientError as e:
        ev["list_buckets_with_deny_all"] = err_text(e)
    sim = None
    try:
        sim = iam.simulate_principal_policy(PolicySourceArn=arn, ActionNames=["s3:ListAllMyBuckets"])
        ev["simulate_principal_policy"] = [r["EvalDecision"] for r in sim["EvaluationResults"]]
    except ClientError as e:
        ev["simulate_principal_policy"] = err_text(e)
    if ev["list_buckets_with_deny_all"] == "ALLOWED":
        raise Wrong(f"explicit Deny * not enforced | {ev}")
    return ev


# ---------------------------------------------------------------- SES ------
@check("ses.send_and_inspect")
def _(t):
    ses = t.c("ses")
    ses.verify_email_identity(EmailAddress="sender@example.com")
    t0 = time.time()
    poll(lambda: ses.get_identity_verification_attributes(Identities=["sender@example.com"])["VerificationAttributes"]
         .get("sender@example.com", {}).get("VerificationStatus") == "Success", timeout=10, interval=0.25)
    verify_s = round(time.time() - t0, 2)
    subj = "m50-" + RUN
    mid = ses.send_email(Source="sender@example.com", Destination={"ToAddresses": ["to@example.com"]},
                         Message={"Subject": {"Data": subj}, "Body": {"Text": {"Data": "body"}}})["MessageId"]
    ev = {"message_id": mid, "seconds_until_identity_Success": verify_s}
    try:
        ses.send_email(Source="unverified@example.org", Destination={"ToAddresses": ["to@example.com"]},
                       Message={"Subject": {"Data": "x"}, "Body": {"Text": {"Data": "x"}}})
        ev["unverified_sender"] = "ACCEPTED (AWS sandbox: MessageRejected)"
    except ClientError as e:
        ev["unverified_sender"] = err_text(e)
    insp = {}
    for path in ("/_ministack/ses/messages", "/_aws/ses", "/__admin/ses/messages", "/__admin/ses"):
        st, body = t.http(t.endpoint + path)
        insp[path] = st
        if st == 200 and subj.encode() in body:
            ev["inspect_endpoint"] = path
            break
    ev["inspect_probe_status"] = insp
    try:
        q = ses.get_send_quota()
        ev["send_quota_sent_24h"] = q.get("SentLast24Hours")
    except ClientError as e:
        ev["send_quota"] = err_text(e)
    if "inspect_endpoint" not in ev:
        raise Wrong(f"send OK but no way found to inspect sent mail | {ev}")
    return ev


# ------------------------------------------------------------- Athena ------
@check("athena.select_1", timeout=45)
def _(t):
    a = t.c("athena")
    b = n("ath")
    t.c("s3").create_bucket(Bucket=b)
    qid = a.start_query_execution(QueryString="SELECT 1 AS one, 'x' AS s",
                                  ResultConfiguration={"OutputLocation": f"s3://{b}/out/"})["QueryExecutionId"]
    st = poll(lambda: (lambda s: s if s["State"] not in ("QUEUED", "RUNNING") else None)(
        a.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]), timeout=20)
    ev = {"state": st and st.get("State"), "reason": st and st.get("StateChangeReason")}
    if not st or st["State"] != "SUCCEEDED":
        raise Wrong(str(ev))
    rows = a.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    ev["rows"] = [[c.get("VarCharValue") for c in r["Data"]] for r in rows]
    if ev["rows"][-1] != ["1", "x"]:
        raise Wrong(str(ev))
    return ev


@check("athena.query_s3_csv", timeout=60)
def _(t):
    """Real SQL over data in S3: CREATE EXTERNAL TABLE on a CSV, then aggregate."""
    a = t.c("athena")
    s3 = t.c("s3")
    b = n("athd")
    s3.create_bucket(Bucket=b)
    s3.put_object(Bucket=b, Key="data/readings.csv", Body=b"dev-1,10\ndev-1,20\ndev-2,5\n")
    out = f"s3://{b}/out/"
    db = "m50db_" + RUN
    ctx = {}
    def run(q):
        qid = a.start_query_execution(QueryString=q, ResultConfiguration={"OutputLocation": out}, **ctx)["QueryExecutionId"]
        st = poll(lambda: (lambda s: s if s["State"] not in ("QUEUED", "RUNNING") else None)(
            a.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]), timeout=20)
        if not st or st["State"] != "SUCCEEDED":
            raise Wrong(f"{q[:60]} -> {st}")
        return [[c.get("VarCharValue") for c in r["Data"]] for r in a.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]]
    tbl = "m50_" + RUN
    notes = {}
    try:
        run(f"CREATE EXTERNAL TABLE {tbl}_d (a string) LOCATION 's3://{b}/data/'")  # implicit 'default' db (exists on AWS)
        notes["implicit_default_db"] = "ok"
    except (Wrong, ClientError) as e:
        notes["implicit_default_db"] = str(e)[:160]
    t.c("glue").create_database(DatabaseInput={"Name": db})  # how Terraform/CDK usually create it
    ctx["QueryExecutionContext"] = {"Database": db}
    run(f"CREATE EXTERNAL TABLE {tbl} (device string, watts int) ROW FORMAT DELIMITED FIELDS TERMINATED BY ',' "
        f"LOCATION 's3://{b}/data/'")
    rows = run(f"SELECT device, SUM(watts) AS total FROM {tbl} GROUP BY device ORDER BY device")
    ev = {"rows": rows, **notes}
    if rows[1:] != [["dev-1", "30"], ["dev-2", "5"]]:
        raise Wrong(f"aggregate over S3 CSV wrong | {ev}")
    return ev


# ------------------------------------------------------------ Bedrock ------
@check("bedrock.converse", timeout=30)
def _(t):
    br = t.c("bedrock-runtime", read_timeout=20)
    r = br.converse(modelId="anthropic.claude-3-haiku-20240307-v1:0",
                    messages=[{"role": "user", "content": [{"text": "Say ok"}]}])
    txt = r["output"]["message"]["content"][0].get("text")
    return {"text": txt[:200], "stopReason": r.get("stopReason"), "usage": r.get("usage")}


# ------------------------------------------------ extra cheap checks -------
@check("logs.put_get")
def _(t):
    lg = t.c("logs")
    g = "/m50/" + RUN + uuid.uuid4().hex[:4]
    lg.create_log_group(logGroupName=g)
    lg.create_log_stream(logGroupName=g, logStreamName="s")
    lg.put_log_events(logGroupName=g, logStreamName="s", logEvents=[{"timestamp": int(time.time() * 1000), "message": "hello-logs"}])
    ev = lg.get_log_events(logGroupName=g, logStreamName="s")["events"]
    if not ev or ev[0]["message"] != "hello-logs":
        raise Wrong(str(ev))
    return {"events": len(ev)}


@check("kinesis.put_get")
def _(t):
    k = t.c("kinesis")
    s = n("ks")
    k.create_stream(StreamName=s, ShardCount=1)
    poll(lambda: k.describe_stream(StreamName=s)["StreamDescription"]["StreamStatus"] == "ACTIVE", timeout=15)
    k.put_record(StreamName=s, Data=b"rec", PartitionKey="p")
    sh = k.describe_stream(StreamName=s)["StreamDescription"]["Shards"][0]["ShardId"]
    it = k.get_shard_iterator(StreamName=s, ShardId=sh, ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
    r = k.get_records(ShardIterator=it)["Records"]
    if not r or r[0]["Data"] != b"rec":
        raise Wrong(str(r))
    return {"records": len(r)}


@check("apigw.http_api_lambda_proxy", timeout=60)
def _(t):
    """API Gateway v2 HTTP API -> Lambda proxy, invoked over the emulator's own URL."""
    ag = t.c("apigatewayv2")
    fn, farn = make_function(t, "api")
    api = ag.create_api(Name=n("api"), ProtocolType="HTTP", Target=farn)
    ev = {"ApiEndpoint": api.get("ApiEndpoint"), "ApiId": api["ApiId"]}
    ep = api.get("ApiEndpoint") or ""
    # without a lambda resource policy AWS answers 500 Internal Server Error
    host = ep.split("://")[-1]
    try:
        st0, b0 = t.http(ep + "/", timeout=20)
    except Exception as e:
        st0, b0 = None, repr(e).encode()
    ev["before_add_permission"] = [st0, b0[:120].decode(errors="replace")]
    t.c("lambda").add_permission(FunctionName=fn, StatementId="apigw", Action="lambda:InvokeFunction",
                                 Principal="apigateway.amazonaws.com")
    if ".execute-api." in host:
        # MiniStack style: route on the Host header
        st, body = t.http(t.endpoint + "/", headers={"Host": host}, timeout=20)
        ev.setdefault("tries", []).append(["Host: " + host, st, body[:150].decode(errors="replace")])
        if st == 200 and b"ok" in body:
            ev["working"] = f"{t.endpoint}/ with Host: {host}"
            return ev
    candidates = [f"{api.get('ApiEndpoint')}/", f"{t.endpoint}/restapis/{api['ApiId']}/$default/_user_request_/",
                  f"{t.endpoint}/_aws/execute-api/{api['ApiId']}/"]
    for u in candidates:
        if not u or u.startswith("None"):
            continue
        try:
            st, body = t.http(u, timeout=20)
        except Exception as e:
            st, body = None, repr(e).encode()
        ev.setdefault("tries", []).append([u, st, body[:150].decode(errors="replace")])
        if st == 200 and b"ok" in body:
            ev["working_url"] = u
            return ev
    # Fallback: wire it explicitly (integration + $default route + $default stage), which AWS quick-create does for you
    integ = ag.create_integration(ApiId=api["ApiId"], IntegrationType="AWS_PROXY", IntegrationUri=farn,
                                  PayloadFormatVersion="2.0")["IntegrationId"]
    ag.create_route(ApiId=api["ApiId"], RouteKey="$default", Target=f"integrations/{integ}")
    ag.create_stage(ApiId=api["ApiId"], StageName="$default", AutoDeploy=True)
    st, body = t.http(f"{t.endpoint}/_aws/execute-api/{api['ApiId']}/", timeout=20)
    ev["explicit_wiring_status"] = st
    if st == 200 and b"ok" in body:
        raise Wrong(f"quick-create Target ignored (no route/stage created); works after explicit "
                    f"integration+route+stage at {t.endpoint}/_aws/execute-api/<id>/ | {ev}")
    raise Wrong(f"no reachable invoke URL | {ev}")


# ======================================================= cross-emulator ===
def cross_checks(emu, ms):
    out = {}
    # 1. emulator bucket notification pointing at a queue that exists only in MiniStack
    qname = n("xq")
    ms_url, ms_arn = queue(ms, qname)
    b = n("xb")
    emu.c("s3").create_bucket(Bucket=b)
    try:
        emu.c("s3").put_bucket_notification_configuration(Bucket=b, NotificationConfiguration={
            "QueueConfigurations": [{"QueueArn": ms_arn, "Events": ["s3:ObjectCreated:*"]}]})
        out["emu_notify_to_ms_only_queue_config"] = "ACCEPTED"
        emu.c("s3").put_object(Bucket=b, Key="k", Body=b"x")
        got = drain(ms, ms_url, want=1, timeout=6)
        out["delivered_to_ministack"] = len(got)
    except ClientError as e:
        out["emu_notify_to_ms_only_queue_config"] = err_text(e)
    # 2. same queue name in both: which copy receives?
    emu_url, emu_arn = queue(emu, qname)
    allow_service(emu, emu_url, emu_arn, "s3.amazonaws.com", "arn:aws:s3:::*")
    out["arn_identical"] = emu_arn == ms_arn
    out["arns"] = {"emu": emu_arn, "ms": ms_arn}
    b2 = n("xb")
    emu.c("s3").create_bucket(Bucket=b2)
    emu.c("s3").put_bucket_notification_configuration(Bucket=b2, NotificationConfiguration={
        "QueueConfigurations": [{"QueueArn": emu_arn, "Events": ["s3:ObjectCreated:*"]}]})
    emu.c("s3").put_object(Bucket=b2, Key="k2", Body=b"x")
    out["same_name_delivered_to_emu_copy"] = len([m for m in drain(emu, emu_url, want=2, timeout=6) if "Records" in m["Body"]])
    out["same_name_delivered_to_ms_copy"] = len(drain(ms, ms_url, want=1, timeout=3))
    # 3. reverse: MiniStack bucket -> queue that exists only in emulator
    q2 = n("xq2")
    e_url, e_arn = queue(emu, q2)
    allow_service(emu, e_url, e_arn, "s3.amazonaws.com", "arn:aws:s3:::*")
    b3 = n("xb3")
    ms.c("s3").create_bucket(Bucket=b3)
    try:
        ms.c("s3").put_bucket_notification_configuration(Bucket=b3, NotificationConfiguration={
            "QueueConfigurations": [{"QueueArn": e_arn, "Events": ["s3:ObjectCreated:*"]}]})
        out["ms_notify_to_emu_only_queue_config"] = "ACCEPTED"
        ms.c("s3").put_object(Bucket=b3, Key="k", Body=b"x")
        out["delivered_to_emulator"] = len(drain(emu, e_url, want=1, timeout=6))
    except ClientError as e:
        out["ms_notify_to_emu_only_queue_config"] = err_text(e)
    # 4. An emulator Lambda cannot reach MiniStack (no network); a MiniStack Lambda can reach the emulator over HTTPS
    src = '''
import boto3, os, json
def handler(e, c):
    try:
        s = boto3.client("sqs", endpoint_url=e["endpoint"], verify=e["ca"], region_name="us-east-1",
                         aws_access_key_id=e["k"], aws_secret_access_key=e["s"])
        s.send_message(QueueUrl=e["url"], MessageBody="bridge-from-ministack-lambda")
        return {"ok": True}
    except Exception as ex:
        return {"error": repr(ex)[:300]}
'''
    fn, _a = make_function(ms, "bridge", src=src)
    payload = {"endpoint": emu.endpoint, "ca": emu.cfg["verify"], "k": emu.cfg["key"], "s": emu.cfg["secret"], "url": e_url}
    r = json.loads(ms.c("lambda", read_timeout=60).invoke(FunctionName=fn, Payload=json.dumps(payload))["Payload"].read())
    got = drain(emu, e_url, want=1, timeout=5)
    out["ministack_lambda_to_emulator_sqs"] = {"lambda_result": r, "received_in_emulator": [m["Body"] for m in got]}
    src2 = '''
import urllib.request
def handler(e, c):
    try:
        return {"status": urllib.request.urlopen("http://127.0.0.1:4601/_ministack/health", timeout=3).status}
    except Exception as ex:
        return {"error": repr(ex)[:200]}
'''
    fn2, _b = make_function(emu, "bridge2", src=src2)
    out["emulator_lambda_to_ministack_http"] = json.loads(emu.c("lambda", read_timeout=60).invoke(FunctionName=fn2, Payload=b"{}")["Payload"].read())
    # 5. a Lambda in MiniStack that calls the emulator: possible only if the function gets the emulator endpoint + creds
    out["note"] = ("Each emulator resolves ARNs only against its own in-process store. "
                   "Cross-emulator wiring must be done by application code (a bridge), not by AWS resource config.")
    return out


# ================================================================ runner ===
def run_target(t, only=None):
    res = {}
    for cid, f, to in CHECKS:
        if only and not any(cid.startswith(o) for o in only):
            continue
        t0 = time.time()
        ex = cf.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(f, t)
        try:
            ev = fut.result(timeout=to)
            res[cid] = {"status": "PASS", "evidence": ev}
        except cf.TimeoutError:
            res[cid] = {"status": "TIMEOUT", "error": f"exceeded {to}s"}
        except Exception as e:
            st, txt = classify(e)
            res[cid] = {"status": st, "error": txt}
            if st == "FAIL" and not isinstance(e, ClientError):
                res[cid]["trace"] = traceback.format_exc().splitlines()[-4:]
        ex.shutdown(wait=False)
        res[cid]["seconds"] = round(time.time() - t0, 2)
        print(f"[{t.name}] {cid:34s} {res[cid]['status']:16s} {res[cid]['seconds']:6.2f}s "
              f"{(res[cid].get('error') or '')[:140]}", flush=True)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["emu", "ms", "both"], default="both")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--cross", action="store_true")
    ap.add_argument("--out", default=os.path.join(HERE, "results.json"))
    a = ap.parse_args()
    names = ["emu", "ms"] if a.target == "both" else [a.target]
    targets = {nm: Target(target_config(nm)) for nm in names}
    try:
        with open(a.out) as f:
            results = json.load(f)
    except Exception:
        results = {}
    results.setdefault("meta", {})
    results["meta"].update({"run": RUN, "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "versions": {"aws-emulator": "1.0.589", "ministack": "1.5.44"},
                            "boto3": boto3.__version__})
    if a.cross:
        results["cross"] = cross_checks(targets["emu"], targets["ms"])
        print(json.dumps(results["cross"], indent=1))
    else:
        with cf.ThreadPoolExecutor(max_workers=len(targets)) as ex:
            futs = {nm: ex.submit(run_target, t, a.only) for nm, t in targets.items()}
            for nm, fu in futs.items():
                results.setdefault(nm, {}).update(fu.result())
    with open(a.out, "w") as f:
        json.dump(results, f, indent=1, default=str)
    print("wrote", a.out)


if __name__ == "__main__":
    main()

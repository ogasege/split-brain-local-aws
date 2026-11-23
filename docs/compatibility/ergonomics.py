#!/usr/bin/env python3
"""Test-automation ergonomics: startup time, idle RSS, reset without restart, persistence across restart,
credential rotation on restart. Uses start.sh/stop.sh (ports 9301/4601 only). Writes results.json["ergonomics"]."""
import os, sys
for k in list(os.environ):
    if k.startswith("AWS_"):
        del os.environ[k]
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1"
import json, re, subprocess, time
sys.argv = [sys.argv[0]]
HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "probe.py")).read().split("# ================================================================ runner ===")[0]
exec(src)


def sh(cmd, env_extra=""):
    return subprocess.run(f"cd {HERE} && {env_extra} ./{cmd}", shell=True, capture_output=True, text=True).stdout


def start(which, extra=""):
    out = sh(f"start.sh {which}", f"MS_EXTRA_ENV='{extra}'" if extra else "")
    m = re.search(r"(emu|ms)_startup_ms=(\d+)", out)
    return int(m.group(2)) if m else None


def stop(which):
    sh(f"stop.sh {which}")
    time.sleep(0.5)


def rss(which):
    if which == "emu":
        pid = subprocess.run("pgrep -f 'aws-emulator -p 9301'", shell=True, capture_output=True, text=True).stdout.split()
    else:
        pid = []
        for p in subprocess.run("pgrep -f 'bin/ministack$'", shell=True, capture_output=True, text=True).stdout.split():
            try:
                if "GATEWAY_PORT=4601" in open(f"/proc/{p}/environ").read().split("\0"):
                    pid.append(p)
            except OSError:
                pass
    if not pid:
        return None
    return int(open(f"/proc/{pid[0]}/status").read().split("VmRSS:")[1].split()[0]) // 1024


out = {}
for which in ("emu", "ms"):
    stop(which)
    times, rsss = [], []
    for _ in range(3):
        times.append(start(which))
        time.sleep(3)
        rsss.append(rss(which))
        stop(which)
    out[f"{which}_startup_ms"] = times
    out[f"{which}_idle_rss_mb_after_3s"] = rsss

# ---- reset without restart
start("ms")
ms = Target(target_config("ms"))
for i in range(3):
    ms.c("s3").create_bucket(Bucket=n("r"))
before = len(ms.c("s3").list_buckets()["Buckets"])
t0 = time.time()
st, body = ms.http(ms.endpoint + "/_ministack/reset", method="POST")
reset_ms = int((time.time() - t0) * 1000)
after = len(ms.c("s3").list_buckets()["Buckets"])
out["ms_reset"] = {"endpoint": "POST /_ministack/reset", "status": st, "ms": reset_ms, "buckets_before": before,
                   "buckets_after": after, "body": body[:200].decode(errors="replace")}
# ---- persistence across restart (default config)
ms.c("s3").create_bucket(Bucket="m50-persist-check")
stop("ms")
start("ms")
ms = Target(target_config("ms"))
out["ms_restart_default_keeps_bucket"] = "m50-persist-check" in [b["Name"] for b in ms.c("s3").list_buckets()["Buckets"]]
stop("ms")
# ---- persistence with PERSIST_STATE=1
start("ms", "PERSIST_STATE=1")
ms = Target(target_config("ms"))
ms.c("s3").create_bucket(Bucket="m50-persist-check2")
ms.c("s3").put_object(Bucket="m50-persist-check2", Key="k", Body=b"persisted")
url, _ = queue(ms, "m50-persist-q")
ms.c("sqs").send_message(QueueUrl=url, MessageBody="survive?")
tn, _ = ddb_table(ms)
ms.c("dynamodb").put_item(TableName=tn, Item={"pk": {"S": "a"}, "sk": {"S": "b"}})
stop("ms")
start("ms", "PERSIST_STATE=1")
ms = Target(target_config("ms"))
pers = {}
try:
    pers["s3_object"] = ms.c("s3").get_object(Bucket="m50-persist-check2", Key="k")["Body"].read().decode()
except Exception as e:
    pers["s3_object"] = repr(e)[:150]
try:
    pers["sqs_message"] = [m["Body"] for m in drain(ms, ms.c("sqs").get_queue_url(QueueName="m50-persist-q")["QueueUrl"], 1, 3)]
except Exception as e:
    pers["sqs_message"] = repr(e)[:150]
try:
    pers["ddb_item"] = "Item" in ms.c("dynamodb").get_item(TableName=tn, Key={"pk": {"S": "a"}, "sk": {"S": "b"}})
except Exception as e:
    pers["ddb_item"] = repr(e)[:150]
pers["state_dir_files"] = sorted(os.listdir("/tmp/mark50-ministack-state"))[:20] if os.path.isdir("/tmp/mark50-ministack-state") else []
out["ms_restart_PERSIST_STATE_1"] = pers
stop("ms")

# ---- emulator: no reset route; restart is the reset; credentials rotate
start("emu")
emu = Target(target_config("emu"))
emu.c("s3").create_bucket(Bucket="m50-persist-check")
probe_routes = {}
for p in ("/__admin/reset", "/_ministack/reset", "/__admin/state"):
    probe_routes[p] = emu.http(emu.endpoint + p, method="POST")[0]
out["emu_reset_routes_probed"] = probe_routes
old_key = emu.cfg["key"]
stop("emu")
start("emu")
new = Target(target_config("emu"))
out["emu_restart_keeps_bucket"] = "m50-persist-check" in [b["Name"] for b in new.c("s3").list_buckets()["Buckets"]]
out["emu_key_rotated_on_restart"] = new.cfg["key"] != old_key
try:
    emu._clients.clear()
    emu.c("s3").list_buckets()
    out["emu_old_key_after_restart"] = "accepted"
except ClientError as e:
    out["emu_old_key_after_restart"] = err_text(e)
stop("emu")

r = json.load(open(os.path.join(HERE, "results.json")))
r["ergonomics"] = out
json.dump(r, open(os.path.join(HERE, "results.json"), "w"), indent=1, default=str)
print(json.dumps(out, indent=1))

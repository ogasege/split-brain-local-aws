#!/usr/bin/env python3
"""Render the check table from results.json and splice it into MATRIX.md between markers."""
import json, os, re
H = os.path.dirname(os.path.abspath(__file__))
r = json.load(open(os.path.join(H, "results.json")))
SHORT = {"PASS": "PASS", "FAIL": "FAIL", "WRONG_SEMANTICS": "WRONG", "NOT_IMPLEMENTED": "501", "TIMEOUT": "TIMEOUT", "SKIP": "SKIP"}


def brief(v):
    ev = v.get("evidence") if v["status"] == "PASS" else v.get("error")
    s = json.dumps(ev, default=str) if not isinstance(ev, str) else ev
    s = s.replace("|", "/").replace("\n", " ")
    return s[:230] + ("..." if len(s) > 230 else "")


ids = list(dict.fromkeys(list(r.get("emu", {})) + list(r.get("ms", {}))))
lines = ["| Check | AWS Emulator 1.0.589 | MiniStack 1.5.44 |", "|---|---|---|"]
for cid in ids:
    cells = []
    for t in ("emu", "ms"):
        v = r.get(t, {}).get(cid)
        cells.append("n/a" if not v else f"**{SHORT[v['status']]}** {brief(v)}")
    lines.append(f"| `{cid}` | {cells[0]} | {cells[1]} |")
tot = {t: {} for t in ("emu", "ms")}
for t in tot:
    for v in r.get(t, {}).values():
        tot[t][v["status"]] = tot[t].get(v["status"], 0) + 1
lines.append("")
lines.append(f"Totals: AWS Emulator {tot['emu']} / MiniStack {tot['ms']} (run {r['meta']['run']}, {r['meta']['when']}, boto3 {r['meta']['boto3']})")
table = "\n".join(lines)
p = os.path.join(H, "MATRIX.md")
md = open(p).read()
md = re.sub(r"<!-- TABLE START -->.*<!-- TABLE END -->", "<!-- TABLE START -->\n" + table + "\n<!-- TABLE END -->", md, flags=re.S)
open(p, "w").write(md)
print("ok", tot)

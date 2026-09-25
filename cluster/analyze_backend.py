#!/usr/bin/env python3
"""analyze_backend.py - gap analysis on records collected by receiver.py.

Usage: python analyze_backend.py records.jsonl logguard.jsonl [hwm_seq]

Groups records by (pod uid, restart, container), finds sequence gaps, counts
duplicates and escrow-recovered lines, and compares the byte count logguard
reported with the bytes implied by the missing lines.
"""
import collections, json, re, sys

SEQ = re.compile(rb'"seq":\s*(\d+)')
UID = re.compile(rb'"uid":\s*"([^"]+)"')
streams = collections.defaultdict(lambda: collections.Counter())
recovered = collections.Counter()
line_bytes = []
with open(sys.argv[1], "rb") as f:
    for raw in f:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        log = (rec.get("log") or "").encode()
        m, u = SEQ.search(log), UID.search(log)
        if not m:
            continue
        key = u.group(1).decode() if u else "?"
        s = int(m.group(1))
        streams[key][s] += 1
        if str(rec.get("tag", "")).startswith("escrow") or "escrow-recovered" in str(rec):
            recovered[key] += 1
        line_bytes.append(len(log) + 41)
hwm = int(sys.argv[3]) if len(sys.argv) > 3 else None
report = {}
for key, c in streams.items():
    top = max(c)
    last = max(top, hwm or -1)
    missing = [s for s in range(0, last + 1) if s not in c]
    report[key] = dict(max_seq_seen=top, hwm=hwm, lines_expected=last + 1,
                       lines_missing=len(missing), tail_missing=max(0, (hwm or top) - top),
                       duplicates=sum(v - 1 for v in c.values() if v > 1),
                       recovered_by_escrow=recovered[key],
                       missingness_ratio=len(missing) / (last + 1))
audit = 0
try:
    for l in open(sys.argv[2]):
        if l.startswith("{"):
            audit += json.loads(l).get("unread_bytes", 0)
except (IndexError, FileNotFoundError):
    pass
mean_line = sum(line_bytes) / len(line_bytes) if line_bytes else 0
print(json.dumps(dict(streams=report, logguard_unread_bytes=audit,
                      mean_line_bytes_on_disk=round(mean_line, 1)), indent=1))

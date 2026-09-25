#!/usr/bin/env python3
"""loggen.py - sequence-marked log generator for the cluster evaluation.

Writes JSON lines to stdout at RATE bytes/s. Each line carries the pod UID,
restart count, container name, and a monotonic sequence number. The last
emitted sequence number is exported on :9102/metrics as a gauge, so a watcher
can detect tail loss that no later line reveals (Section 6.3).
"""
import http.server, itertools, json, os, sys, threading, time

RATE = float(os.environ.get("RATE_BYTES", "8000000"))
LINE = int(os.environ.get("LINE_BYTES", "216"))   # payload; runtime adds the CRI prefix
UID = os.environ.get("POD_UID", "unknown")
RESTART = os.environ.get("RESTART_COUNT", "0")
NAME = os.environ.get("CONTAINER_NAME", "app")
last = -1

class M(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (f'log_seq_emitted{{pod_uid="{UID}",container="{NAME}",restart="{RESTART}"}} {last}\n').encode()
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

threading.Thread(target=http.server.ThreadingHTTPServer(("", 9102), M).serve_forever, daemon=True).start()
head = {"uid": UID, "rc": int(RESTART), "c": NAME, "seq": 0, "msg": ""}
pad = LINE - len(json.dumps(head)) - 12
out = sys.stdout.buffer
t0, sent = time.time(), 0
for seq in itertools.count():
    out.write((json.dumps({"uid": UID, "rc": int(RESTART), "c": NAME, "seq": seq, "msg": "x" * pad}) + "\n").encode())
    last = seq
    sent += LINE
    if seq % 256 == 0:
        out.flush()
        ahead = sent / RATE - (time.time() - t0)
        if ahead > 0:
            time.sleep(ahead)

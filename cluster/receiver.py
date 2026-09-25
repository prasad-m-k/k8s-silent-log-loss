#!/usr/bin/env python3
"""receiver.py - stands in for the log backend. Appends every record it gets to /data/records.jsonl."""
import http.server, os
os.makedirs("/data", exist_ok=True)
out = open("/data/records.jsonl", "ab", buffering=0)
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        out.write(self.rfile.read(n).rstrip(b"\n") + b"\n")
        self.send_response(201); self.end_headers()
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("", 8080), H).serve_forever()

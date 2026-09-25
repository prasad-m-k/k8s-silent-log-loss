#!/usr/bin/env python3
"""
smoke_test.py - real-time check of logguard.py on a local filesystem.

Three threads stand in for the node: a writer (container runtime) at W bytes/s,
a rotator that follows the kubelet's rotation sequence every second, and a slow
tail shipper that reads at R < W bytes/s, keeps rotated files open for
rotate_wait seconds and records offsets in a table shaped like Fluent Bit's
in_tail_files. logguard.py runs as a separate process against the same
directory and database.

Pass criteria
  1. Every sequence number the writer produced reaches either the shipper
     output or the escrow drain output (no loss with escrow on).
  2. The auditor's reported unread bytes equal the bytes the shipper
     actually failed to read, within one poll interval of reading.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "sim"))
from nodesim import make_lines, decode_seqs, LINE_LEN  # noqa: E402

W, R, S = 2_000_000, 1_000_000, 1_000_000
RUN, MAX_FILES, ROTATE_WAIT = 10.0, 3, 1.0

root = tempfile.mkdtemp(prefix="logguard-smoke-")
pods = os.path.join(root, "pods")
cdir = os.path.join(pods, "ns_pod-a_uid1", "app")
os.makedirs(cdir)
active = os.path.join(cdir, "0.log")
db_path = os.path.join(root, "flb.db")
db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE in_tail_files (id INTEGER PRIMARY KEY, name TEXT, offset INTEGER,"
           " inode INTEGER, created INTEGER, rotated INTEGER DEFAULT 0)")
lock = threading.Lock()
state = dict(seq=0, wfd=os.open(active, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640),
             stop=False, shipped=bytearray(), shipper_unread=0)


def writer():
    t0 = time.time()
    while time.time() - t0 < RUN:
        n = int(W * 0.01 / LINE_LEN)
        with lock:
            os.write(state["wfd"], make_lines(state["seq"], n, time.time() - t0))
            state["seq"] += n
        time.sleep(0.01)


def rotator():
    idx = 0
    while not state["stop"]:
        time.sleep(1.0)
        with lock:
            if os.stat(active).st_size < S:
                continue
            names = sorted(n for n in os.listdir(cdir) if n.startswith("0.log."))
            keep = MAX_FILES - 2
            for n in names[:max(len(names) - keep, 0)]:
                os.unlink(os.path.join(cdir, n))
            for n in names[max(len(names) - keep, 0):]:
                if not n.endswith(".gz"):
                    open(os.path.join(cdir, n) + ".gz", "wb").close()
                    os.unlink(os.path.join(cdir, n))
            idx += 1
            os.rename(active, os.path.join(cdir, "0.log.%04d" % idx))
            os.close(state["wfd"])
            state["wfd"] = os.open(active, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)


def shipper():
    tracked = {}   # ino -> [fd, off, rotated_at, rowid]
    while not state["stop"] or tracked:
        try:
            cur = os.stat(active).st_ino
        except FileNotFoundError:
            cur = None
        if cur and cur not in tracked and not state["stop"]:
            fd = os.open(active, os.O_RDONLY)
            rid = db.execute("INSERT INTO in_tail_files (name, offset, inode, created) VALUES (?,?,?,?)",
                             (active, 0, cur, int(time.time()))).lastrowid
            tracked[cur] = [fd, 0, None, rid]
        budget = int(R * 0.01)
        for ino, f in list(tracked.items()):
            if f[2] is None and ino != cur:
                f[2] = time.time()
            size = os.fstat(f[0]).st_size
            n = min(size - f[1], budget)
            n -= n % LINE_LEN
            if n > 0:
                state["shipped"] += os.pread(f[0], n, f[1])
                f[1] += n
                budget -= n
                db.execute("UPDATE in_tail_files SET offset=? WHERE id=?", (f[1], f[3]))
            if f[2] is not None and time.time() >= f[2] + ROTATE_WAIT:
                state["shipper_unread"] += os.fstat(f[0]).st_size - f[1]
                os.close(f[0])
                db.execute("DELETE FROM in_tail_files WHERE id=?", (f[3],))
                del tracked[ino]
        if state["stop"]:
            for ino, f in list(tracked.items()):
                f[2] = f[2] or 0
        time.sleep(0.01)


def main():
    agent = subprocess.Popen([sys.executable, os.path.join(HERE, "logguard.py"),
                              "--pods-root", pods, "--escrow-dir", os.path.join(root, "escrow"),
                              "--drain-dir", os.path.join(root, "recovered"),
                              "--shipper-db", db_path, "--poll", "0.05",
                              "--metrics-file", os.path.join(root, "metrics.prom"),
                              "--duration", str(RUN + 8)],
                             stdout=subprocess.PIPE, text=True)
    time.sleep(0.5)
    ts = [threading.Thread(target=f) for f in (writer, rotator, shipper)]
    for t in ts:
        t.start()
    ts[0].join()
    time.sleep(1.5)
    with lock:
        # container exits: kubelet removes the pod directory (as on eviction)
        state["stop"] = True
        os.close(state["wfd"])
    time.sleep(ROTATE_WAIT + 0.5)
    shutil.rmtree(os.path.dirname(cdir))
    ts[1].join()
    ts[2].join()
    out, _ = agent.communicate()
    events = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
    shipped = set(decode_seqs(bytes(state["shipped"])).tolist())
    rec = bytearray()
    rdir = os.path.join(root, "recovered")
    for n in os.listdir(rdir):
        rec += open(os.path.join(rdir, n), "rb").read()
    recovered = set(decode_seqs(bytes(rec)).tolist())
    total = state["seq"]
    missing_ship = total - len(shipped)
    missing_both = total - len(shipped | recovered)
    audit_unread = sum(e["unread_bytes"] for e in events)
    true_unread = missing_ship * LINE_LEN
    result = dict(lines_written=total, lines_shipped=len(shipped),
                  lines_lost_without_escrow=missing_ship, lines_lost_with_escrow=missing_both,
                  duplicate_lines=len(shipped & recovered),
                  files_finalized=len(events),
                  never_tracked_files=sum(e["never_tracked"] for e in events),
                  auditor_unread_bytes=audit_unread, true_unread_bytes=true_unread,
                  auditor_error_pct=round(100 * (audit_unread - true_unread) / true_unread, 3) if true_unread else 0.0,
                  metrics=open(os.path.join(root, "metrics.prom")).read().split("\n")[1:4])
    print(json.dumps(result, indent=1))
    ok = missing_both == 0 and missing_ship > 0 and abs(result["auditor_error_pct"]) < 5
    print("PASS" if ok else "FAIL")
    shutil.rmtree(root, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

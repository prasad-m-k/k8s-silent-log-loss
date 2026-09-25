#!/usr/bin/env python3
"""
logguard.py - node agent prototype for the offset auditor and log escrow.

Runs as a privileged DaemonSet with /var/log mounted from the host. No change
to applications, the kubelet, or the shipper is required.

Offset auditor
  Watches /var/log/pods with inotify (plus a timed rescan, because inotify
  drops events when its queue overflows). For every container log file it
  records the last size it saw. It reads the shipper's saved offsets from
  Fluent Bit's SQLite table in_tail_files (the shipper must run with
  DB.locking false so other readers can open the file). When a file has no
  name left under /var/log/pods and no row left in the shipper's table, the
  auditor reports size - offset as bytes the shipper never read. Files the
  shipper never opened report their whole size.

Log escrow
  Hard-links every container log file into --escrow-dir as soon as it is
  seen. The kubelet can still rename, compress and delete its own names;
  the inode survives through the escrow link. When the shipper stops
  tracking a file that it had not finished, or never tracked it at all, the
  agent copies the unread tail into --drain-dir, which the shipper tails
  with a separate input, and then removes the escrow link. A byte cap
  bounds how much otherwise-freed data escrow may hold.

Holding a link on every tracked inode has a second effect: the filesystem
cannot reuse that inode number while the agent tracks it, so an inode is an
unambiguous key for the whole time it matters.

Output
  One JSON object per finalized file on stdout, and Prometheus text-format
  counters in --metrics-file (for the node-exporter textfile collector).

This is research code. It has been exercised by smoke_test.py on a local
filesystem, not on a production cluster.
"""

import argparse
import ctypes
import ctypes.util
import json
import os
import select
import sqlite3
import struct
import sys
import time

# ---- inotify via libc ------------------------------------------------------
IN_MODIFY, IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x2, 0x40, 0x80, 0x100, 0x200
IN_Q_OVERFLOW, IN_ISDIR, IN_NONBLOCK = 0x4000, 0x40000000, 0o4000
_libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
_EVENT = struct.Struct("iIII")


class Inotify:
    MASK = IN_CREATE | IN_MOVED_TO | IN_MOVED_FROM | IN_DELETE

    def __init__(self):
        self.fd = _libc.inotify_init1(IN_NONBLOCK)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.wd = {}

    def add(self, path):
        if path in self.wd.values():
            return
        wd = _libc.inotify_add_watch(self.fd, path.encode(), self.MASK)
        if wd >= 0:
            self.wd[wd] = path

    def read(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            buf = os.read(self.fd, 65536)
        except BlockingIOError:
            return []
        out, i = [], 0
        while i < len(buf):
            wd, mask, _cookie, ln = _EVENT.unpack_from(buf, i)
            name = buf[i + 16:i + 16 + ln].rstrip(b"\0").decode()
            out.append((self.wd.get(wd), mask, name))
            i += 16 + ln
        return out


# ---- agent -----------------------------------------------------------------
class LogGuard:
    def __init__(self, a):
        self.a = a
        self.files = {}      # ino -> dict(path, pod, container, size, off, seen_db, esc, queued)
        self.inotify = Inotify()
        self.counters = dict(lost_bytes=0, lost_files=0, never_tracked_files=0,
                             escrow_recovered_bytes=0, escrow_overflow_bytes=0,
                             escrow_link_fail=0, rescans=0, overflows=0)
        os.makedirs(a.escrow_dir, exist_ok=True)
        os.makedirs(a.drain_dir, exist_ok=True)
        self.next_rescan = 0.0

    # container log files are /var/log/pods/<ns>_<pod>_<uid>/<container>/<n>.log[.<ts>]
    @staticmethod
    def is_log(name):
        return ".log" in name and not name.endswith((".gz", ".tmp"))

    def scan(self):
        present = {}
        root = self.a.pods_root
        for pod in os.listdir(root) if os.path.isdir(root) else []:
            pdir = os.path.join(root, pod)
            self.inotify.add(pdir)
            try:
                containers = os.listdir(pdir)
            except FileNotFoundError:
                continue
            for c in containers:
                cdir = os.path.join(pdir, c)
                if not os.path.isdir(cdir):
                    continue
                self.inotify.add(cdir)
                try:
                    names = os.listdir(cdir)
                except FileNotFoundError:
                    continue
                for n in names:
                    if not self.is_log(n):
                        continue
                    p = os.path.join(cdir, n)
                    try:
                        st = os.stat(p)
                    except FileNotFoundError:
                        continue
                    present[st.st_ino] = (p, pod, c, st.st_size)
        return present

    def shipper_rows(self):
        if not self.a.shipper_db or not os.path.exists(self.a.shipper_db):
            return {}
        try:
            con = sqlite3.connect(f"file:{self.a.shipper_db}?mode=ro", uri=True, timeout=0.2)
            rows = con.execute("SELECT inode, offset FROM in_tail_files").fetchall()
            con.close()
        except sqlite3.Error:
            return None          # locked or busy: skip this round, do not finalize
        out = {}
        for ino, off in rows:
            out[int(ino)] = max(int(off or 0), out.get(int(ino), 0))
        return out

    def escrow_link(self, ino, f):
        dst = os.path.join(self.a.escrow_dir, f"{f['pod']}__{f['container']}__{ino}.log")
        try:
            os.link(f["path"], dst)
            f["esc"] = dst
        except FileExistsError:
            f["esc"] = dst
        except OSError:
            self.counters["escrow_link_fail"] += 1

    def emit(self, **kw):
        kw["ts"] = time.time()
        print(json.dumps(kw), flush=True)

    def finalize(self, ino, f, reason):
        size = f["size"]
        if f.get("esc"):
            try:
                size = max(size, os.stat(f["esc"]).st_size)
            except FileNotFoundError:
                pass
        lost = max(size - f["off"], 0)
        never = not f["seen_db"]
        if lost:
            self.counters["lost_bytes"] += lost
            self.counters["lost_files"] += 1
        if never:
            self.counters["never_tracked_files"] += 1
        self.emit(event="file_final", pod=f["pod"], container=f["container"], inode=ino,
                  size=size, shipper_offset=f["off"], unread_bytes=lost,
                  never_tracked=never, reason=reason, escrowed=bool(f.get("esc")))
        if f.get("esc"):
            if lost:
                self.drain(ino, f, f["off"], size)
            try:
                os.unlink(f["esc"])
            except FileNotFoundError:
                pass
        del self.files[ino]

    def drain(self, ino, f, start, end):
        out = os.path.join(self.a.drain_dir, f"{f['pod']}__{f['container']}__{ino}.recovered.log")
        with open(f["esc"], "rb") as src, open(out, "ab") as dst:
            src.seek(start)
            left = end - start
            while left > 0:
                chunk = src.read(min(left, 1 << 20))
                if not chunk:
                    break
                dst.write(chunk)
                left -= len(chunk)
        self.counters["escrow_recovered_bytes"] += end - start

    def enforce_cap(self):
        if self.a.cap_bytes <= 0:
            return
        held = []
        for ino, f in self.files.items():
            if f.get("esc"):
                try:
                    st = os.stat(f["esc"])
                except FileNotFoundError:
                    continue
                if st.st_nlink == 1:           # the kubelet's own name is gone
                    held.append((f["first_seen"], ino, st.st_size))
        total = sum(s for _, _, s in held)
        for _, ino, size in sorted(held):
            if total <= self.a.cap_bytes:
                break
            f = self.files[ino]
            self.counters["escrow_overflow_bytes"] += max(size - f["off"], 0)
            os.unlink(f["esc"])
            f["esc"] = None
            total -= size

    def tick(self, force_rescan=False):
        present = self.scan()
        rows = self.shipper_rows()
        now = time.time()
        for ino, (p, pod, c, size) in present.items():
            f = self.files.get(ino)
            if f is None:
                f = self.files[ino] = dict(path=p, pod=pod, container=c, size=size, off=0,
                                           seen_db=False, esc=None, first_seen=now)
                if self.a.escrow:
                    self.escrow_link(ino, f)
            f["path"], f["size"] = p, max(size, f["size"])
        if rows is None:
            return
        for ino, off in rows.items():
            if ino in self.files:
                self.files[ino]["off"] = max(off, self.files[ino]["off"])
                self.files[ino]["seen_db"] = True
        for ino in list(self.files):
            if ino not in present and ino not in rows:
                self.finalize(ino, self.files[ino], "name_removed")
        self.enforce_cap()

    def write_metrics(self):
        if not self.a.metrics_file:
            return
        tmp = self.a.metrics_file + ".tmp"
        with open(tmp, "w") as m:
            for k, v in self.counters.items():
                m.write(f"# TYPE logguard_{k}_total counter\nlogguard_{k}_total {v}\n")
            m.write(f"# TYPE logguard_tracked_files gauge\nlogguard_tracked_files {len(self.files)}\n")
        os.replace(tmp, self.a.metrics_file)

    def run(self):
        self.inotify.add(self.a.pods_root)
        end = time.time() + self.a.duration if self.a.duration > 0 else float("inf")
        while time.time() < end:
            events = self.inotify.read(self.a.poll)
            if any(m & IN_Q_OVERFLOW for _, m, _ in events):
                self.counters["overflows"] += 1
            if time.time() >= self.next_rescan:
                self.counters["rescans"] += 1
                self.next_rescan = time.time() + self.a.rescan
            self.tick()
            self.write_metrics()
        # shutting down: report what is still tracked, keep escrow links in place
        self.write_metrics()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pods-root", default="/var/log/pods")
    ap.add_argument("--escrow-dir", default="/var/log/escrow")
    ap.add_argument("--drain-dir", default="/var/log/escrow-recovered")
    ap.add_argument("--shipper-db", default="/var/log/flb_kube.db")
    ap.add_argument("--poll", type=float, default=0.1, help="seconds between offset polls")
    ap.add_argument("--rescan", type=float, default=5.0, help="seconds between full rescans")
    ap.add_argument("--cap-bytes", type=int, default=0, help="0 = no cap")
    ap.add_argument("--no-escrow", dest="escrow", action="store_false")
    ap.add_argument("--metrics-file", default="")
    ap.add_argument("--duration", type=float, default=0, help="seconds to run, 0 = forever")
    a = ap.parse_args()
    LogGuard(a).run()


if __name__ == "__main__":
    sys.exit(main())

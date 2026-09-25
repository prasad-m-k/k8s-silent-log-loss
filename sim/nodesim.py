"""
nodesim.py - node-local emulator for silent container log loss.

The emulator performs real filesystem operations (write, rename, unlink,
hard link, read through open file descriptors) on a scratch directory, but
advances time with a simulated clock. That keeps every run deterministic for
a given seed while exercising the same inode behavior a Kubernetes node has.

Actors, one per role on a node:

  Writer   - the container runtime appending CRI-formatted lines to 0.log.
             Every line carries a monotonic sequence number.
  Kubelet  - rotation logic ported from pkg/kubelet/logs/container_log_manager.go:
             remove excess rotated files (keep MaxFiles-2), compress every
             existing uncompressed rotated file, rename 0.log to
             0.log.<timestamp>, reopen.  Compression is modeled as removal of
             the uncompressed name (the .gz payload is not written).
  Shipper  - a tail input. Two discovery profiles:
               "inotify": rotation detected within detect_interval, the new
                          0.log opened at once (Fluent Bit in_tail behavior).
               "poll":    rotation and new files noticed only every
                          refresh_interval (polling collectors).
             Rotated files are kept open for rotate_wait seconds, then closed
             whether or not bytes remain (Fluent Bit issue #10414).
             Offsets are committed to a real SQLite table shaped like
             Fluent Bit's in_tail_files; rows are deleted when a file closes.
  Auditor  - (Idea 1) scans the pod log directory, keeps the last size of
             every inode, polls the shipper's offset table, and when an inode
             is gone from both reports size - last_offset as bytes lost.
  Escrow   - (Idea 2) hard-links every log inode into an escrow directory,
             releases the link once the shipper has committed past EOF, and
             drains any remainder itself when the shipper gave up or never
             opened the file.  A byte cap bounds the extra disk it holds.
  Backend  - counts deliveries per sequence number, so ground truth,
             duplicates and detected gaps come from the delivered bytes.

Byte quantities can be scaled down (Config.scale) so that a run fits in a
laptop's disk and memory. Loss behavior depends on ratios such as W/R and
S/W, which the scaling keeps fixed; results are reported both scaled and as
unscaled equivalents.
"""

from __future__ import annotations

import os
import random
import shutil
import sqlite3
from dataclasses import dataclass, field, asdict

import numpy as np

LINE_LEN = 256          # bytes per log line, CRI prefix included
PREFIX_LEN = 40         # "2026-09-24THH:MM:SS.nnnnnnnnnZ stdout F "
SEQ_OFF = PREFIX_LEN + len('{"seq":')
SEQ_LEN = 12


def _line_template():
    head_json = '{"seq":%012d,"uid":"pod-a","rc":0,"c":"app","msg":"'
    tail_json = '"}\n'
    pad = LINE_LEN - PREFIX_LEN - len(head_json % 0) - len(tail_json)
    assert pad > 0
    return head_json + ("x" * pad) + tail_json


_TEMPLATE = _line_template()


def make_lines(first_seq: int, n: int, t: float) -> bytes:
    hh = int(t // 3600) % 24
    mm = int(t // 60) % 60
    ss = t % 60
    prefix = "2026-09-24T%02d:%02d:%012.9fZ stdout F " % (hh, mm, ss)
    assert len(prefix) == PREFIX_LEN
    return "".join(prefix + (_TEMPLATE % s) for s in range(first_seq, first_seq + n)).encode()


_POW = (10 ** np.arange(SEQ_LEN - 1, -1, -1)).astype(np.int64)


def decode_seqs(buf: bytes) -> np.ndarray:
    """Sequence numbers of the complete lines in buf (len multiple of LINE_LEN)."""
    if not buf:
        return np.empty(0, dtype=np.int64)
    a = np.frombuffer(buf, dtype=np.uint8).reshape(-1, LINE_LEN)
    digits = a[:, SEQ_OFF:SEQ_OFF + SEQ_LEN].astype(np.int64) - 48
    return digits @ _POW


@dataclass
class Config:
    # workload (unscaled units: bytes, bytes/s, seconds)
    write_rate: float = 8e6            # W
    duration: float = 90.0             # writer active time
    drain: float = 10.0                # minimum time after writer stops
    max_drain: float = 600.0           # stop even if the shipper is still behind
    # kubelet
    max_size: float = 10 * 1024 ** 2   # containerLogMaxSize
    max_files: int = 5                 # containerLogMaxFiles
    monitor_interval: float = 10.0     # containerLogMonitorInterval
    # shipper
    profile: str = "inotify"           # "inotify" or "poll"
    read_rate: float = 32e6            # R, sustained tail throughput
    read_jitter: float = 0.2           # uniform +/- fraction per tick
    rotate_wait: float = 5.0
    detect_interval: float = 0.1       # inotify reaction time
    refresh_interval: float = 10.0     # poll profile
    db_commit_interval: float = 0.02  # Fluent Bit updates the offset after each chunk
    outages: list = field(default_factory=list)  # [(start, end)] shipper paused
    # auditor
    audit_interval: float = 0.1
    # escrow
    escrow: bool = True
    escrow_cap: float = float("inf")   # bytes of otherwise-freed data held
    escrow_read_rate: float = 16e6
    # eviction
    evict_at: float | None = None
    # monitoring
    hwm_scrape_interval: float = 15.0  # app exports last-emitted seq as a gauge
    # engine
    dt: float = 0.02
    scale: float = 16.0                # divide byte quantities by this
    seed: int = 1
    workdir: str = "/tmp/nodesim"


class Node:
    def __init__(self, cfg: Config):
        self.c = cfg
        self.rng = random.Random(cfg.seed)
        s = cfg.scale
        self.W = cfg.write_rate / s
        self.S = cfg.max_size / s
        self.R = cfg.read_rate / s
        self.RE = cfg.escrow_read_rate / s
        self.cap = cfg.escrow_cap / s

        self.root = os.path.join(cfg.workdir, f"run-{os.getpid()}-{cfg.seed}")
        shutil.rmtree(self.root, ignore_errors=True)
        self.poddir = os.path.join(self.root, "var/log/pods/ns_pod-a_uid/app")
        self.escdir = os.path.join(self.root, "var/log/escrow")
        os.makedirs(self.poddir)
        os.makedirs(self.escdir)
        self.active = os.path.join(self.poddir, "0.log")

        self.t = 0.0
        max_lines = int(self.W * (cfg.duration + 5) / LINE_LEN) + 10
        self.recv_ship = np.zeros(max_lines, dtype=np.uint16)
        self.recv_esc = np.zeros(max_lines, dtype=np.uint16)

        # writer
        self.seq = 0
        self.carry_lines = 0.0
        self.wfd = None
        self.ino2fid = {}          # live inode -> unique file id (inodes get reused)
        self.next_fid = 0
        self.seen_inodes = set()
        self.inode_reuses = 0
        self.written = {}          # fid -> bytes written
        self.first_seq = {}        # ino -> first seq in that file
        self.writer_alive = True
        self._open_writer()

        # kubelet
        self.next_monitor = self.rng.uniform(0, cfg.monitor_interval)
        self.rot_idx = 0
        self.rotations = 0
        self.peak_active = 0

        # shipper
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE in_tail_files (id INTEGER PRIMARY KEY, name TEXT,"
                        " offset INTEGER, inode INTEGER, created INTEGER, rotated INTEGER DEFAULT 0)")
        self.tracked = {}          # ino -> dict(fd, read, carry, rotated_at, row, committed)
        self.shipped_read = {}     # ino -> bytes read by shipper (ground truth)
        self.ever_tracked = set()
        self.next_detect = 0.0
        self.next_refresh = 0.0
        self.next_commit = cfg.db_commit_interval
        self.abandoned_bytes = 0
        self.abandoned_files = 0
        self.first_abandon_t = None
        self.read_budget = 0.0

        # auditor
        self.a_size = {}           # ino -> last seen size
        self.a_off = {}            # ino -> last seen committed offset
        self.a_seen_db = set()
        self.a_final = {}          # ino -> estimated lost bytes
        self.next_audit = 0.0

        # escrow
        self.esc = {}              # ino -> dict(path, size)
        self.esc_queue = []        # [ino, pos] awaiting drain
        self.esc_fd = {}
        self.esc_overflow_bytes = 0
        self.esc_peak_extra = 0
        self.esc_link_miss = 0
        self.esc_budget = 0.0

        # monitoring
        self.hwm = -1
        self.next_scrape = 0.0
        self.evicted = False

    # ---------------------------------------------------------------- writer
    def _open_writer(self):
        if self.wfd is not None:
            os.close(self.wfd)
        self.wfd = os.open(self.active, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        ino = os.fstat(self.wfd).st_ino
        self.next_fid += 1
        if ino in self.seen_inodes:
            self.inode_reuses += 1
        self.seen_inodes.add(ino)
        self.ino2fid[ino] = self.next_fid
        self.wfid = self.next_fid
        self.written[self.wfid] = 0
        self.first_seq[self.wfid] = self.seq

    def _write(self):
        if not self.writer_alive or self.t >= self.c.duration:
            return
        self.carry_lines += self.W * self.c.dt / LINE_LEN
        n = int(self.carry_lines)
        if n <= 0:
            return
        self.carry_lines -= n
        data = make_lines(self.seq, n, self.t)
        os.write(self.wfd, data)
        self.written[self.wfid] += len(data)
        self.seq += n

    # --------------------------------------------------------------- kubelet
    def _kubelet(self):
        if self.evicted or self.t < self.next_monitor:
            return
        self.next_monitor += self.c.monitor_interval
        try:
            size = os.stat(self.active).st_size
        except FileNotFoundError:
            return
        if size < self.S:
            return
        names = sorted(n for n in os.listdir(self.poddir) if n.startswith("0.log."))
        keep = max(self.c.max_files - 2, 0)
        for n in names[:max(len(names) - keep, 0)]:
            os.unlink(os.path.join(self.poddir, n))
        for n in names[max(len(names) - keep, 0):]:
            if not n.endswith(".gz"):
                p = os.path.join(self.poddir, n)
                open(p + ".gz", "wb").close()   # placeholder for the gzip copy
                os.unlink(p)
        self.rot_idx += 1
        os.rename(self.active, os.path.join(self.poddir, "0.log.%08d" % self.rot_idx))
        self.rotations += 1
        if self.writer_alive:
            self._open_writer()

    # --------------------------------------------------------------- shipper
    def _paused(self):
        return any(a <= self.t < b for a, b in self.c.outages)

    def _track(self, path):
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            return
        ino = self.ino2fid[os.fstat(fd).st_ino]
        if ino in self.ever_tracked:
            os.close(fd)
            return
        cur = self.db.execute("INSERT INTO in_tail_files (name, offset, inode, created) VALUES (?,?,?,?)",
                              (path, 0, ino, int(self.t)))
        self.tracked[ino] = dict(fd=fd, read=0, carry=b"", rotated_at=None,
                                 row=cur.lastrowid, committed=0)
        self.shipped_read.setdefault(ino, 0)
        self.ever_tracked.add(ino)

    def _active_ino(self):
        try:
            return self.ino2fid[os.stat(self.active).st_ino]
        except FileNotFoundError:
            return None

    def _shipper_discover(self):
        c = self.c
        if c.profile == "inotify":
            if self.t < self.next_detect:
                return
            self.next_detect += c.detect_interval
        else:
            if self.t < self.next_refresh:
                return
            self.next_refresh += c.refresh_interval
        cur = self._active_ino()
        for ino, f in self.tracked.items():
            if f["rotated_at"] is None and ino != cur:
                f["rotated_at"] = self.t
                self.db.execute("UPDATE in_tail_files SET rotated=1 WHERE id=?", (f["row"],))
        if cur is not None and cur not in self.tracked and cur not in self.ever_tracked:
            self._track(self.active)

    def _deliver(self, ino, f, data, sink):
        buf = f["carry"] + data
        n = len(buf) // LINE_LEN
        full, f["carry"] = buf[:n * LINE_LEN], buf[n * LINE_LEN:]
        if n:
            seqs = decode_seqs(full)
            np.add.at(sink, seqs, 1)

    def _shipper_read(self):
        if self._paused():
            self.read_budget = 0.0
            return
        j = 1 + self.rng.uniform(-self.c.read_jitter, self.c.read_jitter)
        self.read_budget += self.R * self.c.dt * j
        order = sorted(self.tracked.items(),
                       key=lambda kv: (kv[1]["rotated_at"] is None, kv[1]["rotated_at"] or 0))
        for ino, f in order:
            if self.read_budget < 1:
                break
            size = os.fstat(f["fd"]).st_size
            avail = size - f["read"]
            if avail <= 0:
                continue
            n = int(min(avail, self.read_budget))
            data = os.pread(f["fd"], n, f["read"])
            f["read"] += len(data)
            self.shipped_read[ino] += len(data)
            self.read_budget -= len(data)
            self._deliver(ino, f, data, self.recv_ship)
        self.read_budget = min(self.read_budget, self.R * self.c.dt * 2)

    def _shipper_close(self):
        for ino in list(self.tracked):
            f = self.tracked[ino]
            if f["rotated_at"] is not None and self.t >= f["rotated_at"] + self.c.rotate_wait:
                size = os.fstat(f["fd"]).st_size
                if f["read"] < size or f["carry"]:
                    if self.first_abandon_t is None:
                        self.first_abandon_t = self.t
                    self.abandoned_files += 1
                    self.abandoned_bytes += size - f["read"] + len(f["carry"])
                os.close(f["fd"])
                self.db.execute("DELETE FROM in_tail_files WHERE id=?", (f["row"],))
                del self.tracked[ino]

    def _shipper_commit(self):
        if self.t < self.next_commit:
            return
        self.next_commit += self.c.db_commit_interval
        for f in self.tracked.values():
            f["committed"] = f["read"] - len(f["carry"])
            self.db.execute("UPDATE in_tail_files SET offset=? WHERE id=?", (f["committed"], f["row"]))

    # --------------------------------------------------------------- auditor
    def _scan_dir(self):
        out = {}
        try:
            for e in os.scandir(self.poddir):
                if e.name.endswith(".gz"):
                    continue
                st = e.stat()
                out[self.ino2fid[st.st_ino]] = st.st_size
        except FileNotFoundError:
            pass
        return out

    def _auditor(self, force=False):
        if not force and self.t < self.next_audit:
            return
        self.next_audit += self.c.audit_interval
        present = self._scan_dir()
        for ino, sz in present.items():
            self.a_size[ino] = max(sz, self.a_size.get(ino, 0))
        rows = {ino: off for ino, off in self.db.execute("SELECT inode, offset FROM in_tail_files")}
        for ino, off in rows.items():
            self.a_off[ino] = max(off, self.a_off.get(ino, 0))
            self.a_seen_db.add(ino)
        for ino in list(self.a_size):
            if ino in self.a_final:
                continue
            gone = ino not in present
            if force or (gone and ino not in rows):
                self.a_final[ino] = max(self.a_size[ino] - self.a_off.get(ino, 0), 0)

    # ---------------------------------------------------------------- escrow
    def _escrow(self):
        if not self.c.escrow:
            return
        present = self._scan_dir()
        for ino in present:
            if ino not in self.esc and ino not in self.a_final:
                src = None
                for n in os.listdir(self.poddir):
                    p = os.path.join(self.poddir, n)
                    try:
                        if not n.endswith(".gz") and self.ino2fid[os.stat(p).st_ino] == ino:
                            src = p
                            break
                    except FileNotFoundError:
                        pass
                dst = os.path.join(self.escdir, f"pod-a_app_{ino}.log")
                try:
                    os.link(src, dst)
                    self.esc[ino] = dict(path=dst, queued=False)
                except (FileNotFoundError, TypeError):
                    self.esc_link_miss += 1
        cur = self._active_ino()
        rows = {ino: off for ino, off in self.db.execute("SELECT inode, offset FROM in_tail_files")}
        for ino, e in list(self.esc.items()):
            # Still the active file, or the shipper still holds it: wait.
            # A rotated file the shipper never opened cannot be discovered
            # later (the tail glob matches only the active name), so it is
            # handled at once.
            if e["queued"] or ino == cur or ino in rows:
                continue
            size = os.stat(e["path"]).st_size
            start = self.a_off.get(ino, 0) if ino in self.ever_tracked else 0
            if start >= size:
                os.unlink(e["path"])
                del self.esc[ino]
            else:
                e["queued"] = True
                self.esc_queue.append([ino, start])
        self._escrow_cap(present)
        self._escrow_drain()

    def _escrow_extra_bytes(self, present):
        extra = 0
        for ino, e in self.esc.items():
            if ino in present or ino in self.tracked:
                continue
            extra += os.stat(e["path"]).st_size
        return extra

    def _escrow_cap(self, present):
        extra = self._escrow_extra_bytes(present)
        while extra > self.cap and self.esc_queue:
            ino, pos = self.esc_queue.pop(0)
            e = self.esc.pop(ino)
            size = os.stat(e["path"]).st_size
            self.esc_overflow_bytes += size - pos
            extra -= size
            os.unlink(e["path"])
            if ino in self.esc_fd:
                os.close(self.esc_fd.pop(ino))
        self.esc_peak_extra = max(self.esc_peak_extra, extra)

    def _escrow_drain(self):
        self.esc_budget += self.RE * self.c.dt
        while self.esc_queue and self.esc_budget >= LINE_LEN:
            ino, pos = self.esc_queue[0]
            e = self.esc[ino]
            if ino not in self.esc_fd:
                self.esc_fd[ino] = os.open(e["path"], os.O_RDONLY)
            fd = self.esc_fd[ino]
            size = os.fstat(fd).st_size
            n = int(min(size - pos, self.esc_budget))
            n -= n % LINE_LEN
            if n > 0:
                data = os.pread(fd, n, pos)
                np.add.at(self.recv_esc, decode_seqs(data), 1)
                self.esc_queue[0][1] += n
                self.esc_budget -= n
            if self.esc_queue[0][1] >= size:
                os.close(self.esc_fd.pop(ino))
                os.unlink(e["path"])
                del self.esc[ino]
                self.esc_queue.pop(0)
            elif n <= 0:
                break
        self.esc_budget = min(self.esc_budget, self.RE * self.c.dt * 2)

    # ------------------------------------------------------------- eviction
    def _evict(self):
        if self.c.evict_at is None or self.evicted or self.t < self.c.evict_at:
            return
        self.evicted = True
        self.writer_alive = False
        os.close(self.wfd)
        self.wfd = None
        shutil.rmtree(os.path.dirname(self.poddir))

    def _scrape(self):
        if self.t >= self.next_scrape:
            self.next_scrape += self.c.hwm_scrape_interval
            if self.writer_alive:
                self.hwm = self.seq - 1

    # ------------------------------------------------------------------ run
    def _quiescent(self) -> bool:
        """Writer finished, shipper caught up on every open file, escrow idle."""
        if self.t < self.c.duration:
            return False
        for f in self.tracked.values():
            if f["read"] < os.fstat(f["fd"]).st_size or f["carry"]:
                return False
        cur = self._active_ino()
        if cur is not None and cur not in self.tracked and self.written.get(cur, 0) > 0:
            return False
        return not self.esc_queue

    def run(self) -> dict:
        c = self.c
        i = 0
        self.truncated = False
        while True:
            self.t = i * c.dt
            self._evict()
            self._write()
            self._scrape()
            self._kubelet()
            if os.path.exists(self.active):
                self.peak_active = max(self.peak_active, os.stat(self.active).st_size)
            self._shipper_discover()
            self._shipper_read()
            self._shipper_commit()
            self._shipper_close()
            self._auditor()
            self._escrow()
            i += 1
            if self.t >= c.duration + c.drain and self._quiescent():
                break
            if self.t >= c.duration + c.max_drain:
                self.truncated = True
                break
        # Close whatever the shipper still holds. After quiescence these files
        # are fully read, so closing them loses nothing.
        for f in self.tracked.values():
            f["rotated_at"] = -1e9
        self._shipper_commit()
        self._auditor()
        self._shipper_close()
        self._auditor()
        self._escrow()
        for _ in range(int(c.max_drain / c.dt)):
            if not self.esc_queue:
                break
            self._escrow_drain()
        self._auditor(force=True)
        return self._report()

    def _report(self) -> dict:
        c = self.c
        n = self.seq
        ship = self.recv_ship[:n] > 0
        both = (self.recv_ship[:n] + self.recv_esc[:n]) > 0
        lost = ~ship
        lost_n = int(lost.sum())
        # watcher: gaps between received sequence numbers
        rec_idx = np.flatnonzero(ship)
        if rec_idx.size:
            internal = int(lost[: rec_idx[-1] + 1].sum())
            tail = int(lost[rec_idx[-1] + 1:].sum())
        else:
            internal, tail = 0, lost_n
        hwm = self.hwm if self.hwm >= 0 else n - 1
        tail_hwm = int(lost[rec_idx[-1] + 1: hwm + 1].sum()) if rec_idx.size else int(lost[:hwm + 1].sum())
        never = [i for i in self.written if i not in self.ever_tracked]
        never_bytes = sum(self.written[i] for i in never)
        true_lost_bytes = lost_n * LINE_LEN
        audit_bytes = int(sum(self.a_final.values()))
        dup = int(((self.recv_ship[:n] > 0) & (self.recv_esc[:n] > 0)).sum()
                  + (self.recv_ship[:n] > 1).sum())
        s = c.scale
        out = dict(
            profile=c.profile, seed=c.seed, scale=s,
            W_MBps=c.write_rate / 1e6, R_MBps=c.read_rate / 1e6,
            rotate_wait=c.rotate_wait, escrow_cap_MB=(c.escrow_cap / 1e6 if c.escrow_cap != float("inf") else -1),
            evict_at=c.evict_at if c.evict_at is not None else -1,
            outage_s=sum(b - a for a, b in c.outages),
            lines_written=n, lines_lost=lost_n, loss_ratio=lost_n / n if n else 0.0,
            lost_MB_equiv=true_lost_bytes * s / 1e6,
            rotations=self.rotations, inode_reuses=self.inode_reuses,
            sim_seconds=round(self.t, 2), truncated=self.truncated,
            peak_active_MB_equiv=self.peak_active * s / 1e6,
            files_written=len(self.written), files_never_tracked=len(never),
            never_tracked_MB_equiv=never_bytes * s / 1e6,
            files_abandoned=self.abandoned_files,
            first_abandon_s=self.first_abandon_t if self.first_abandon_t is not None else -1,
            abandoned_MB_equiv=self.abandoned_bytes * s / 1e6,
            gap_detected_lines=internal, tail_undetected_lines=tail,
            tail_detected_by_hwm=tail_hwm,
            watcher_recall_gaps_only=internal / lost_n if lost_n else 1.0,
            watcher_recall_with_hwm=(internal + tail_hwm) / lost_n if lost_n else 1.0,
            auditor_lost_MB_equiv=audit_bytes * s / 1e6,
            auditor_error_pct=(100.0 * (audit_bytes - true_lost_bytes) / true_lost_bytes) if true_lost_bytes else 0.0,
            escrow_lines_lost=int((~both).sum()),
            escrow_loss_ratio=float((~both).sum()) / n if n else 0.0,
            escrow_recovered_lines=int((both & ~ship).sum()),
            escrow_duplicates=dup,
            escrow_peak_extra_MB_equiv=self.esc_peak_extra * s / 1e6,
            escrow_overflow_MB_equiv=self.esc_overflow_bytes * s / 1e6,
            escrow_link_miss=self.esc_link_miss,
        )
        shutil.rmtree(self.root, ignore_errors=True)
        return out


def run(**kw) -> dict:
    return Node(Config(**kw)).run()


if __name__ == "__main__":
    import json, time
    t0 = time.time()
    r = run(write_rate=16e6, seed=1)
    r["wall_s"] = round(time.time() - t0, 2)
    print(json.dumps(r, indent=1))

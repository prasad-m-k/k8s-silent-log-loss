"""
run_all.py - runs every experiment in the paper and writes results/*.csv.

Usage:
    python experiments/run_all.py            # full run, 5 seeds (~5 min on 1 vCPU)
    python experiments/run_all.py --quick    # 2 seeds, for a smoke test

Each experiment id (E1..E7) matches a table or figure in the paper.
All byte quantities in the CSVs are unscaled equivalents (MB = 1e6 bytes).
"""

import argparse
import csv
import json
import os
import platform
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))

import nodesim            # noqa: E402
import otel_queue         # noqa: E402

RESULTS = os.path.join(ROOT, "results")


def write_csv(name, rows):
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, name)
    keys = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {path} ({len(rows)} rows)")


def sweep(exp, grid, seeds, base=None):
    rows = []
    for params in grid:
        for s in seeds:
            kw = dict(base or {})
            kw.update(params)
            kw["seed"] = s
            r = nodesim.run(**kw)
            r["experiment"] = exp
            for k, v in params.items():
                if k not in r:
                    r[k] = v if not isinstance(v, list) else json.dumps(v)
            rows.append(r)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    seeds = [1, 2] if args.quick else [1, 2, 3, 4, 5]
    t0 = time.time()

    print("E1 write-rate sweep (inotify shipper, R = 32 MB/s)")
    grid = [dict(write_rate=w * 1e6) for w in (8, 16, 24, 32, 40, 48, 64)]
    write_csv("e1_rate_sweep.csv", sweep("E1", grid, seeds))

    print("E2 rotate_wait sweep at W = 48 MB/s (writer runs 300 s)")
    grid = [dict(write_rate=48e6, rotate_wait=w, duration=300.0) for w in (1, 5, 10, 20, 30, 60)]
    write_csv("e2_rotate_wait.csv", sweep("E2", grid, seeds))

    print("E3 polling shipper vs monitor interval, W = 16 MB/s")
    grid = [dict(write_rate=16e6, profile=p, monitor_interval=m)
            for p in ("poll", "inotify") for m in (10, 5, 2, 1)]
    write_csv("e3_poll_monitor.csv", sweep("E3", grid, seeds))

    print("E4 auditor accuracy vs offset-poll interval, W = 48 MB/s")
    grid = [dict(write_rate=48e6, audit_interval=a) for a in (0.02, 0.1, 0.5, 1.0, 2.0)]
    write_csv("e4_auditor_poll.csv", sweep("E4", grid, seeds))

    print("E5 eviction during shipper backpressure, W = 8 MB/s")
    grid = [dict(write_rate=8e6, outages=[(20, 70)], evict_at=e) for e in (30, 40, 50, 60)]
    write_csv("e5_eviction.csv", sweep("E5", grid, seeds))

    print("E6 escrow byte cap, W = 48 MB/s")
    grid = [dict(write_rate=48e6, escrow_cap=c) for c in (250e6, 500e6, 1000e6, 2000e6, float("inf"))]
    write_csv("e6_escrow_cap.csv", sweep("E6", grid, seeds))

    print("E7 direct export queue model")
    rows = []
    for rate in (1000, 5000, 20000):
        for o in (0, 1, 2, 5, 10, 40):
            rows.append(otel_queue.run(otel_queue.OtelConfig(rate=rate, outage=(20.0, 20.0 + o),
                                                             duration=90.0)))
    write_csv("e7_otel_queue.csv", rows)

    env = dict(python=sys.version.split()[0], platform=platform.platform(),
               machine=platform.machine(), processor=platform.processor(),
               cpus=os.cpu_count(), seeds=seeds, wall_seconds=round(time.time() - t0, 1),
               line_len=nodesim.LINE_LEN, scale=nodesim.Config().scale,
               started=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)))
    with open(os.path.join(RESULTS, "environment.json"), "w") as f:
        json.dump(env, f, indent=2)
    print(f"done in {env['wall_seconds']} s")


if __name__ == "__main__":
    main()

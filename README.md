# Silent log loss in Kubernetes: code and data

This repository holds the emulator, experiments, raw results, agent prototype, and cluster kit behind the preprint:

> Prasad MK, "Silent Log Loss in Kubernetes: Mechanisms, Detection, and a Sequence-Marker Solution," preprint, SSRN, 2026.

Every number in Section 10 of the paper comes from the scripts here. If you cite a number, cite the preprint and the commit you ran.

## What is measured, and what is not

| Source | Status |
|---|---|
| Node emulator (`sim/nodesim.py`) | Run. Real file operations on ext4, simulated clock, 5 seeds per condition. |
| Real-time agent test (`agent/smoke_test.py`) | Run 3 times, all passed. Real inotify, real hard links, real SQLite read by a second process. No kubelet, no Fluent Bit. |
| Atomic counter benchmark (`bench/atomic_bench.c`) | Run on 1 vCPU. Says nothing about multi-core contention. |
| Cluster kit (`cluster/`) | Written and syntax-checked. **Not yet run.** |

The emulator's shipper is a model, not Fluent Bit. Section 10.1 of the paper lists what the emulator leaves out: CPU contention, gzip time, the kubelet worker queue across many containers, containerd behavior during rotation, and more than one container per node.

## Quick start

You need Python 3.10 or later. A full run takes about 3.5 minutes on one vCPU.

```bash
pip install -r requirements.txt
python experiments/run_all.py          # writes results/e1..e7 CSVs and environment.json
python analysis/make_figures.py        # writes figures/fig3..fig6
python analysis/make_diagrams.py       # writes figures/fig1, fig2
python agent/smoke_test.py             # real-time agent test, about 20 s, prints PASS or FAIL
gcc -O2 -pthread bench/atomic_bench.c -o bench/atomic_bench && ./bench/atomic_bench 1
```

For a two-seed smoke run of the experiments, use `python experiments/run_all.py --quick`.

## Repository layout

```
sim/nodesim.py          node emulator: writer, kubelet rotation, shipper, auditor, escrow, backend
sim/otel_queue.py       OpenTelemetry SDK batch-processor loss model (direct export)
experiments/run_all.py  runs experiments E1 to E7 and writes results/*.csv
analysis/               figure scripts
agent/logguard.py       node agent prototype: offset auditor + log escrow (inotify, Fluent Bit SQLite)
agent/smoke_test.py     real-time test of logguard.py
bench/atomic_bench.c    cost of the per-line sequence increment
cluster/                kind cluster kit for the evaluation plan (not yet run)
results/                raw CSVs, smoke-test outputs, benchmark output, environment.json
figures/                PNGs used in the paper (300 DPI)
paper/                  manuscript source (content.js, build.js) and equation images
```

## How the emulator works

The emulator writes real files and advances a simulated clock in 20 ms steps. Each run is deterministic for a given seed.

- **Writer.** Appends 256-byte CRI-formatted lines, each with a sequence number.
- **Kubelet.** Follows the rotation sequence in `pkg/kubelet/logs/container_log_manager.go`: remove the oldest rotated files until `MaxFiles - 2` remain, compress every uncompressed rotated file, rename `0.log` to `0.log.<n>`, reopen. Compression is modeled as removing the uncompressed name.
- **Shipper.** Tails the active file, keeps rotated files open for `Rotate_Wait`, and reads at rate R with ±20% jitter. It keeps offsets in an SQLite table shaped like Fluent Bit's `in_tail_files` and deletes a row when it closes a file. It has two discovery profiles:
  - `inotify`: reopens the active path within 100 ms of a rotation.
  - `poll`: notices rotations and new files every `refresh_interval`.
- **Auditor and escrow.** Algorithms 1 and 2 of the paper.
- **Backend.** Counts deliveries per sequence number. Every loss, gap, and duplicate is computed from delivered bytes, not inferred.

Byte quantities are divided by `scale = 16` so runs fit on a laptop. Times are not scaled. The CSVs report unscaled equivalents (MB = 10^6 bytes).

Run a single condition:

```python
import sys; sys.path.insert(0, "sim")
import nodesim
r = nodesim.run(write_rate=48e6, read_rate=32e6, rotate_wait=5, seed=1)
print(r["loss_ratio"], r["auditor_error_pct"], r["escrow_loss_ratio"])
```

`Config` in `nodesim.py` documents every parameter. The most useful ones are:

- `write_rate`, `read_rate`
- `max_size`, `max_files`, `monitor_interval`
- `profile`, `rotate_wait`, `refresh_interval`
- `audit_interval`, `escrow`, `escrow_cap`, `escrow_read_rate`
- `outages`, `evict_at`, `hwm_scrape_interval`, `duration`, `seed`

## Experiments and headline results

All values are means over 5 seeds. Defaults: `containerLogMaxSize` 10 MiB, `MaxFiles` 5, monitor interval 10 s, shipper read rate R = 32 MB/s, `Rotate_Wait` 5 s, offset poll 100 ms.

| ID | File | Question | Result |
|---|---|---|---|
| E1 | `e1_rate_sweep.csv` | Loss against write rate W | 0% up to 32 MB/s; 14.4%, 28.4%, 46.1% at 40, 48, 64 MB/s. With uncapped escrow: 0% at every rate. |
| E2 | `e2_rotate_wait.csv` | Does Rotate_Wait prevent loss? (W = 48, 300 s) | First loss at (w + 1.5 s) / (1 - R/W): 7.4 s at w = 1 s, 184.4 s at w = 60 s. Loss still 19.6% to 32.8%. |
| E3 | `e3_poll_monitor.csv` | Shorter monitor interval with a polling shipper (W = 16) | Polling shipper lost 0%, 50.0%, 78.7%, 89.3% at 10, 5, 2, 1 s. Event-driven shipper lost 0% at every interval. |
| E4 | `e4_auditor_poll.csv` | Auditor error against offset poll interval (W = 48) | Over-count 0.5%, 2.1%, 7.7%, 9.9%, 23.6% at 0.02, 0.1, 0.5, 1, 2 s. |
| E5 | `e5_eviction.csv` | Detecting eviction loss | Gap check found 0%. The 15 s gauge found 0% to 83%. The auditor measured 99.4% to 99.9% of lost bytes. Escrow lost 0%. |
| E6 | `e6_escrow_cap.csv` | Escrow byte cap (W = 48) | A cap of 500 MB or more lost 0%. A 250 MB cap (below one rotated file) left 13.9% loss. |
| E7 | `e7_otel_queue.csv` | Direct export drops during a collector outage | Drops about equal to r × d − 2,560 below the 30 s timeout. At 20,000 records/s, a 1 s outage drops 17,688 records. |

Real-time agent test, 3 runs (`results/smoke_test_run*.txt`):

- 76,596 lines written per run.
- 35,919 to 35,958 lines lost without escrow; 0 lost with escrow.
- Auditor over-count: +1.4% to +1.8%.
- Duplicate lines: 507 to 663.

Atomic increment: 6.4 ns (relaxed fetch-add, 1 vCPU, Intel Xeon 2.10 GHz).

### CSV columns (E1 to E6)

| Column | Meaning |
|---|---|
| `loss_ratio` | Share of written lines never delivered by the shipper (R_m in the paper) |
| `first_abandon_s` | Time the shipper first closed a file with unread bytes (-1 = never) |
| `files_abandoned`, `files_never_tracked` | Files closed with unread bytes; files the shipper never opened |
| `watcher_recall_gaps_only` | Share of lost lines inside a gap that a later line reveals |
| `watcher_recall_with_hwm` | Same, plus tail losses found by the high-water gauge |
| `auditor_error_pct` | (auditor lost bytes − true lost bytes) / true lost bytes × 100 |
| `escrow_loss_ratio` | Share of lines lost with escrow enabled |
| `escrow_duplicates` | Lines delivered by both the shipper and the escrow drain |
| `escrow_peak_extra_MB_equiv` | Largest amount of otherwise-freed data escrow held |
| `escrow_overflow_MB_equiv` | Data released by the cap before it was read |
| `peak_active_MB_equiv` | Largest size the active `0.log` reached (rotation lag) |
| `inode_reuses` | Times a new `0.log` received an inode number used earlier |

## The agent prototype

`agent/logguard.py` is the offset auditor and log escrow in one process. It is meant to run as a privileged DaemonSet with `/var/log` mounted from the host (`cluster/logguard.yaml`).

- It watches `/var/log/pods` with inotify plus a timed rescan.
- It reads Fluent Bit's offset table. Fluent Bit must run with `DB.locking false`.
- It hard-links every container log file into `/var/log/escrow`. The escrow directory must be on the same filesystem as `/var/log/pods`.
- It copies unread tails into `/var/log/escrow-recovered`, which Fluent Bit tails as a second input.
- It prints one JSON object per finished file.
- It writes Prometheus counters to `--metrics-file` for the node-exporter textfile collector.

It is research code. It reads only Fluent Bit's offset store. Filebeat's registry and the OpenTelemetry Collector's file-log checkpoints would each need their own reader.

## Cluster evaluation (for the journal version)

`cluster/run_experiment.sh <rate_bytes_per_s> <seconds> [rotation|eviction|burst]` does the following:

1. Creates a kind cluster with the kubelet settings in `kind-config.yaml`.
2. Deploys Fluent Bit, logguard, a receiver, and a sequence-marked generator that exports a high-water gauge on `:9102`.
3. Runs the scenario.
4. Collects `records.jsonl`, logguard events, kubelet logs, and cluster events into `results/cluster/<timestamp>-<scenario>-<rate>/`.
5. Runs `analyze_backend.py`.

It needs docker, kind 0.23 or later, and kubectl. **It has not been run.** Expect to adjust it on first use: image tags, the eviction fill size, and Fluent Bit's CRI parser output format.

Success criteria are fixed in Section 11 of the paper:

- Gap-detection recall, counting the gauge: 95% or higher.
- Classification accuracy: 90% or higher.
- False-positive rate over a 72-hour soak: below 1%.
- Auditor error against sequence-derived loss: within 5%.
- Escrow below its cap: 0 lost lines, and duplicates below 1% at a 100 ms poll.

Report R_m together with the median and 95th-percentile gap size for every condition.

## Rebuilding the manuscript

```bash
npm install docx
python paper/eqs.py                    # equation images
node paper/build.js                    # writes the .docx
```

`build.js` reads figures from `figures/` and equations from `paper/eq/`. Adjust the two path constants at its top if you move them.

## Related work to read before extending this

- Fluent Bit PR #12361 adds in-shipper abandoned-byte counters. It uses the same arithmetic as the auditor but excludes files the shipper never opened.
- Vector issue #24675 proposes the same kind of counter.
- Kubernetes issue #129975 describes intermediate files lost between collector polls.
- Kubelet source `pkg/kubelet/logs/container_log_manager.go` defines the rotation sequence the emulator ports.

## License and citation

The code is MIT licensed (`LICENSE`). See `CITATION.cff` for citation details.

The companion paper on source-side log encoding (log stenography) cites this preprint.

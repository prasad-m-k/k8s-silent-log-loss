// content.js - manuscript text. Markup: `code`, *italic*, **bold**, [@key] citations.
const B = [];
const h1 = (t) => B.push({ t: "h1", x: t });
const h2 = (t) => B.push({ t: "h2", x: t });
const p = (x) => B.push({ t: "p", x });
const fig = (file, cap, w) => B.push({ t: "fig", file, cap, w: w || 6.5 });
const eq = (file, n, h) => B.push({ t: "eq", file, n, h: h || 0.42 });
const table = (cap, head, rows, widths, note) => B.push({ t: "table", cap, head, rows, widths, note });
const algo = (title, lines) => B.push({ t: "algo", title, lines });
const list = (items) => B.push({ t: "list", items });

h1("1. Introduction");
p("Log rotation on a single Linux host is a solved problem. You know the file, you know the inode, and you can watch both. Kubernetes changes this because the component that rotates and deletes container logs is neither the application nor a sidecar the application controls. It is the kubelet, a node-level process that acts on per-container size limits and node-wide disk pressure that the application cannot query.");
p("When a database on a virtual machine fills its log partition, the write fails and something raises an error. When the kubelet rotates a container's log past the point a shipper has read, or evicts the pod to reclaim disk, the application keeps running. It never held a file descriptor for its own log, because the container runtime captures stdout and stderr underneath it. The data is gone, and no component that took part in losing it reports the loss.");
p("Teams build alerting on the assumption that missing data produces a missing-data signal. Here it usually does not. A gap in a service's logs during a traffic spike, a deploy, or a cascading failure is common enough that platform engineers often treat it as noise. Those windows are the ones an incident review needs most.");
p("This paper makes five contributions.");
list([
  "It describes three loss mechanisms (rotation lag, disk-pressure eviction, and the race between kubelet rotation and the log shipper) using upstream issue reports and the kubelet's own rotation code. The code shows that a rotated file keeps an uncompressed name for exactly one rotation interval, which sets the window a shipper has to open it.",
  "It specifies a detection layer built on application-level sequence markers, adds a high-water gauge for losses at the end of a stream that no later line can reveal, and replaces the draft decision table with ordered classification rules.",
  "It proposes an offset auditor that measures lost bytes on the node without any application change, by comparing each log file's final size with the shipper's saved offset when both the file and the shipper's record of it are gone. Unlike shipper self-metrics, it also counts files the shipper never opened.",
  "It proposes log escrow, which hard-links every container log file into a separate directory so that the kubelet's rotation cleanup and pod teardown cannot free data the shipper has not read, and drains any unread tail itself.",
  "It measures all of the above on a node emulator that ports the kubelet's rotation sequence and performs real file operations, tests the agent prototype in real time, and publishes the code, raw results, and a cluster evaluation kit."
]);
p("No result in this paper comes from a running cluster. The emulator numbers depend on its shipper model, and Section 10 states where that model may differ from Fluent Bit or Vector in production. The cluster evaluation in Section 11 has fixed success criteria and has not been run.");

h1("2. Background: the Kubernetes log path");
fig("fig1_log_path.png", "Fig. 1. The log path on a node and where the parts proposed in this paper attach. The application's responsibility ends at its write to stdout. The runtime writes the file, the kubelet renames, compresses, and deletes it, and the shipper tails it on its own schedule. The logguard agent (gold) watches the pod log directory, reads the shipper's saved offsets, measures unread bytes, and holds hard links until the data is read. The dashed path is direct export through an OpenTelemetry SDK, which bypasses the file.");
p("The container runtime, containerd or CRI-O, performs every write to the log file. The kubelet does not touch that write path. It owns the file's lifecycle: deciding when the file is too large, rotating it, and deleting old files [@k8slog]. The distinction matters when choosing which component's logs to read first after a gap.");
p("Two kubelet settings govern rotation. `containerLogMaxSize` sets the size at which the active file rotates (default 10Mi), and `containerLogMaxFiles` sets how many files a container may keep (default 5) [@k8slog][@kubeletcfg]. Two further settings, added through pull request #114301 and available in the kubelet configuration since Kubernetes 1.30, control how the rotation loop runs: `containerLogMaxWorkers` (default 1) sets how many rotations may run at once, and `containerLogMonitorInterval` (default 10s) sets how often the kubelet checks file sizes [@pr114301][@kubeletcfg][@bottlerocket].");
h2("2.1. What one rotation does");
p("The rotation sequence in `pkg/kubelet/logs/container_log_manager.go` is short, and its order has consequences [@clm]. On each monitor tick the kubelet queues every running container. A worker stats the active log and returns if the file is below the size limit. Otherwise it lists the rotated files, removes leftover temporary files, removes the oldest rotated files until at most `MaxFiles - 2` remain, gzip-compresses every rotated file that is still uncompressed and removes the uncompressed original, renames `0.log` to `0.log.<YYYYMMDD-HHMMSS>`, and asks the runtime to reopen the log.");
p("Two consequences follow. First, only the newest rotated file is ever uncompressed, and it loses its uncompressed name at the next rotation. A shipper that follows `*.log` names therefore has one rotation interval to open a rotated file. An open descriptor keeps the data readable past that point; a name does not. The kubelet source itself notes that the newest rotated file is left uncompressed so that a log agent can finish reading it [@clm]. Second, compression runs inside the rotation worker. With the default single worker, gzip work for one large file delays the size check for every other container on the node. This paper did not measure that effect; it is a plausible contributor to the rotation lag described next.");
p("None of this reaches the application. There is no event, webhook, or environment variable that says a log just rotated or that the node just deleted part of a container's history. *Rotation lag* in this paper means the delay between the moment a file crosses its size limit and the moment the kubelet rotates it.");

h1("3. Three mechanisms of silent log loss");
table("Table 1. Loss mechanisms, what triggers them, and the signal available today.",
  ["Mechanism", "Trigger", "Deletes data by itself?", "Signal available today"],
  [
    ["Rotation lag", "Write rate W high relative to the monitor interval; many log-heavy pods per node", "Rarely. It makes files larger, which enlarges the other two losses", "Kubelet debug logs; file size far above containerLogMaxSize"],
    ["Disk-pressure eviction", "nodefs.available < 10% or imagefs.available < 15% (defaults)", "Yes. The pod's log directory is removed with the pod", "DiskPressure node condition; Evicted pod event"],
    ["Shipper race", "Shipper reads slower than W, or misses a rotation between polls", "Yes. Unread bytes are dropped when the shipper closes the file or the kubelet removes the name", "Shipper self-metrics where they exist; nothing for files never opened"],
  ], [1.3, 1.9, 1.6, 1.7]);
h2("3.1. Rotation lag under sustained writes");
p("Kubernetes issue #110630 describes a StatefulSet writing about 6 MB/s with `containerLogMaxSize` at 50Mi and `containerLogMaxFiles` at 5 [@i110630]. It should rotate every eight or nine seconds and hold about 250 MB. On a node running several heavy loggers, the kubelet fell behind and the files grew to tens of gigabytes before rotation caught up. Issue #134324 reports a separate failure in the timestamp-based rotation introduced in Kubernetes 1.27: above roughly 50 MB/s the rotation task never runs, and `0.log` grows without bound [@i134324]. The reporter had already set `containerLogMonitorInterval` to 1s and `containerLogMaxWorkers` to 10, so tuning those two settings did not prevent it.");
p("Even without a stalled worker, rotation happens only on a monitor tick, so the active file can pass the limit by up to one interval of writes:");
eq("eq2.png", 1, 0.26);
p("where *S* is `containerLogMaxSize`, *W* the write rate, and *I_m* the monitor interval. In the emulator of Section 10, a container writing 48 MB/s with default settings reached 479 MB before rotating, 46 times the 10Mi limit. Rotation lag alone rarely deletes data. It makes every later loss larger: a shipper that abandons one rotated file now abandons hundreds of megabytes, and the node's disk fills faster.");
h2("3.2. Disk-pressure eviction");
p("The kubelet's default hard eviction thresholds are `nodefs.available < 10%` and `imagefs.available < 15%`, with matching inode thresholds [@k8sevict]. When log growth pushes a node past them, the kubelet evicts pods, starting with those whose usage exceeds their requests. For hard thresholds it ends the pod without the normal termination grace period [@k8sevict], so the application gets no chance to flush.");
p("The Kubernetes documentation states that when a pod is evicted, its containers are evicted along with their logs [@k8slog]. Rotation leaves old data on disk until file-count limits reach it; eviction removes the directory at once. The Evicted pod object remains visible in the API for a while, but it holds status, not log content. If the shipper had not forwarded the lines, and shippers batch by design, they are gone.");
p("Eviction loss has a shape that matters for detection. The lost lines are the last ones the container wrote before it died, so no later line arrives to reveal the gap. Section 6.3 returns to this.");
h2("3.3. The shipper race");
p("This mechanism has the most field evidence. Fluent Bit issue #2110 reports a container that wrote one million lines under load, of which the tail input collected about 200,000, all from the first file [@fb2110]. Fluent Bit issue #10414 describes a burst of about 300 MB in one second: the kubelet rotated every 10 s, the 5 s `Rotate_Wait` expired before the shipper finished the rotated file, and the unread bytes were dropped [@fb10414]. Fluentd daemonset issue #465 and Kubernetes issue #129975 report the other form: when the kubelet rotates twice before the collector notices, the collector moves to the newest file and never reads the one in between [@fd465][@i129975]. Cribl's documentation for its Kubernetes log source gives the same warning for polling collectors [@cribl].");
p("The two forms have different causes. The first is throughput: the shipper opened the file but could not drain it before it stopped tracking it. The second is discovery: the shipper never opened the file. Fluent Bit's rotation handler reopens the original path as soon as it sees its tracked file renamed [@fbtailfile], which avoids most discovery misses. Collectors that discover files by polling do not.");
p("Inode reuse adds a third complication. Shippers key their saved offsets by inode; Fluent Bit's tail database stores an inode and an offset per file [@fbtail][@fbsql]. When the kubelet deletes a rotated file, its inode number can be handed to the next `0.log`. In the emulator on ext4, a new active file reused an inode number freed earlier in about 9 of every 10 rotations. The first version of the emulator keyed its own bookkeeping by inode and reported 65% loss in a run that had none. Any tool that tracks log files by inode alone needs a second key or a way to pin the inode.");

h1("4. Existing mitigations and one trade-off");
p("Teams have partial defenses today. Raising `containerLogMaxSize` lowers the rotation frequency and moves the point at which rotation lag starts to matter; it does not remove it. Setting ephemeral-storage requests and limits per pod lets the kubelet act on one pod before node-wide pressure forces a blunt eviction. Rotation-aware shipper settings, such as a longer `Rotate_Wait`, give the shipper more time with a rotated file. Shipper self-metrics, where they exist, show some losses. And `kubectl logs` reads only the current file, so it cannot serve as an audit trail for anything already rotated or evicted [@k8slog].");
p("Two of these have limits that the emulator made concrete. A longer `Rotate_Wait` delays loss but does not prevent it when the write rate exceeds the shipper's read rate (Section 10.3). Shortening `containerLogMonitorInterval`, a documented fix for rotation lag, makes rotations more frequent, and with a polling collector that refreshes every 10 s it raised loss to between 50% and 89% of lines at 16 MB/s (Section 10.4). An event-driven shipper lost nothing under the same settings. The right value for this setting depends on how the shipper discovers files.");
p("None of these defenses tells an operator that a gap has already happened in a specific stream, how large it was, or why. The rest of this paper addresses that.");

h1("5. Related work and why existing detection falls short");
h2("5.1. Shipper self-metrics");
p("Shippers count what they handle. Vector's `files_unwatched_total` counts files it stopped watching before end of file, but not the bytes left behind; issue #24675 proposes a byte counter for that case [@vec24675]. Fluent Bit pull request #12361, opened in September 2026 and not merged at the time of writing, adds `fluentbit_input_files_processed_bytes_total` and `fluentbit_input_files_abandoned_bytes_total` to the tail input, computing abandoned bytes as file size minus the resumable offset when a tracked file is removed [@fbpr12361]. That is the same arithmetic as the offset auditor in Section 7, applied inside the shipper. Its author states that it does not cover files lost before discovery. The OpenTelemetry Collector reports refused and failed records for data that reached it [@otelcol]. All of these count data the pipeline saw. None can count a file it never opened.");
h2("5.2. Keeping deleted files readable");
p("Several shippers keep a deleted file readable by holding its descriptor open. Filebeat can hold descriptors of deleted files while its output is blocked [@beats2395]. A Vector fork added `rotate_wait_ms`, whose default keeps descriptors of deleted files open indefinitely [@vecviaq]. Alibaba's Logtail keeps a file open while parsing is blocked so that deletion does not lose data [@logtail]. Fluent Bit's `Rotate_Wait` does the same for a fixed time [@fbtail]. These approaches tie the data's lifetime to one shipper process. A restart or an out-of-memory kill releases every held file; a 2026 benchmark observed Fluent Bit and Filebeat being OOM-killed under load [@vmbench]. They also cover only files the shipper opened. Log escrow (Section 8) moves the hold to a separate agent, pins files the shipper never opened, and puts a node-wide cap on the bytes held.");
h2("5.3. Sequence-based gap detection");
p("Sequence numbers as a continuity check are not new. Kafka consumers track offsets per partition [@kafka], and tamper-evident logging uses counters and hash chains to prove that no record was removed [@crosby]. The borrowed part of this paper is gap detection. The part that is specific to Kubernetes is tying a gap to its node-level cause using signals that exist only there: kubelet events, node conditions, and the shipper's file state.");
p("File-based rotation detection assumes the detector can inspect a file: an inode, an offset, or content it can re-read. Eviction removes that possibility, because nothing remains to inspect. The question becomes whether data existed that no remaining artifact can show. Sequence numbers answer it from the application side; the offset auditor answers it from the node side by recording sizes before the files disappear.");
table("Table 2. Approaches to log loss on a Kubernetes node.",
  ["Approach", "App change", "Sees files the shipper never opened", "Survives shipper restart", "Effect"],
  [
    ["Shipper byte counters [@fbpr12361][@vec24675]", "No", "No", "Counter resets", "Measures, bytes"],
    ["Shipper holds open descriptors [@beats2395][@vecviaq][@logtail]", "No", "No", "No", "Delays loss"],
    ["Sequence markers + watcher (Sec. 6)", "Yes", "Yes, as gaps", "Yes", "Detects, lines"],
    ["High-water gauge (Sec. 6.3)", "Yes", "Yes, tail gaps", "Yes", "Detects, lines"],
    ["Offset auditor (Sec. 7)", "No", "Yes", "Yes", "Measures, bytes"],
    ["Log escrow (Sec. 8)", "No", "Yes", "Yes", "Prevents up to a cap"],
    ["Direct export (Sec. 9)", "Yes", "Not applicable", "Collector-side", "Avoids the file"],
  ], [2.2, 0.8, 1.35, 1.05, 1.1]);

h1("6. Sequence-marker continuity watching");
h2("6.1. The marker");
p("Each log line, or every Nth line, carries a monotonically increasing sequence number generated in the application or its logging wrapper. Timestamps cannot tell \"nothing happened\" from \"something happened and was lost\"; a counter can. The counter needs identity fields to survive normal cluster behavior. The pod UID and the container's restart count separate a restarted container, whose counter starts again at zero, from a gap in a running one. The container name separates streams in multi-container pods, since each container writes its own file. A per-process instance identifier settles which layer owns the counter when a logging library attaches fields per logger instance.");
p("The increment must be atomic. A service writing tens of megabytes per second almost always logs from many threads, and a plain increment on a shared integer can produce duplicates or skipped numbers that look like loss. Go's `sync/atomic` and Java's `java.util.concurrent.atomic` provide lock-free increments. A relaxed atomic fetch-and-add in C measured 6.4 ns per increment on one vCPU of a 2.1 GHz Xeon (`bench/atomic_bench.c`, best of five runs of 50 million increments). That machine had one vCPU, so the figure says nothing about contention across cores.");
h2("6.2. The watcher and the missingness ratio");
p("A continuity watcher reads the stream after it reaches the backend and checks each (pod UID, restart count, container) key for gaps. Fig. 2 shows the two kinds of gap it can meet. The watcher reports the missingness ratio for a stream or a test condition as");
eq("eq1.png", 2, 0.44);
p("together with the median and 95th-percentile gap size. At very high line rates the watcher can sample every Nth marker, at the cost of gap-size precision; a first implementation should check every line and make sampling a setting.");
fig("fig2_gap_types.png", "Fig. 2. Two kinds of gap. An internal gap is visible because a later line (10415) arrives. A tail gap has no later line, which is the usual case for eviction: the container is gone. A watcher that only compares consecutive sequence numbers cannot see it.");
h2("6.3. Tail gaps and the high-water gauge");
p("A gap check needs a later line to reveal a missing range. When the lost lines are the last a container wrote, as with eviction or with a shipper that abandons the final rotated file, no later line exists. In the eviction experiments of Section 10.7, the gap check alone found 0% of the lost lines in every run.");
p("The fix is to publish the last emitted sequence number outside the log path. The application exports it as a gauge, for example `log_seq_emitted{pod_uid, container, restart}`, which Prometheus scrapes. The watcher compares the highest sequence number received with the gauge's last value and reports the difference as a tail gap. Lines emitted after the last scrape remain invisible, so the gauge's coverage depends on scrape timing: with a 15 s scrape it found between 0% and 83% of eviction losses in the emulator, depending on where the eviction fell relative to the scrape. The offset auditor covers the remainder in bytes.");
h2("6.4. Classification");
p("A gap is not proof of silent loss. A container that crashes or is OOM-killed also stops mid-stream. The draft version of this work used a decision table whose rows overlapped; the rules below are ordered, and the first rule that matches decides. Each rule is evaluated over the gap's time window and the stream's pod and container.");
table("Table 3. Classification rules, applied in order.",
  ["#", "Condition in the gap window", "Classification"],
  [
    ["1", "Container restart, OOM kill, or non-zero exit", "Application termination, not silent loss"],
    ["2", "Evicted event for the pod, or the auditor reports the pod log directory removed", "Eviction loss (DiskPressure recorded as context)"],
    ["3", "Auditor reports unread bytes for a file the shipper never opened", "Shipper race, discovery miss"],
    ["4", "Auditor reports unread bytes for a tracked file whose final size exceeds 2 x containerLogMaxSize", "Rotation lag amplifying a shipper race"],
    ["5", "Auditor reports unread bytes for a tracked file", "Shipper race, drain miss"],
    ["6", "No node signal; collector reports refused or failed records", "Downstream or direct-export loss"],
    ["7", "More than one of rules 2 to 6, or none", "Ambiguous, flag for review"],
  ], [0.35, 3.9, 2.25]);
p("Rule 1 matters as much as the others. Without it, every crash-looping pod on a busy cluster would count as a false positive for one of the loss mechanisms. Rules 3 to 5 depend on the offset auditor. Without it, the classifier falls back to the draft's weaker evidence: DiskPressure without an eviction suggests rotation lag, and a gap in the shipper's own file-open and file-close counts suggests a shipper race.");

h1("7. The offset auditor");
h2("7.1. Design");
p("The offset auditor measures bytes lost on a node without touching the application, the kubelet, or the shipper. It runs as a DaemonSet with the host's `/var/log` mounted. It watches `/var/log/pods` with inotify and also rescans the tree on a timer, because inotify drops events when its queue overflows and reports that only as an `IN_Q_OVERFLOW` event [@inotify]. For each container log file it records the largest size it has seen. On a fixed poll interval it reads the shipper's saved offsets. For Fluent Bit these live in the SQLite table `in_tail_files`, with columns for name, offset, inode, creation time, and a rotated flag [@fbsql]; the shipper must run with `DB.locking false` so another process can read the database [@fbtail].");
p("A file is finished when two things are true: no name for it remains under `/var/log/pods`, and the shipper no longer has a row for it. At that point the kubelet has deleted or compressed it and the shipper has stopped reading it. The auditor then reports its unread bytes, and the node total over a set of finished files *F* is");
eq("eq5.png", 3, 0.42);
p("where *s_i* is the file's last recorded size and *o_i* the last offset the auditor saw for it. A file the shipper never opened has no offset and counts in full. As an example, a 52,428,800-byte rotated file whose shipper offset stopped at 31,457,280 lost 20,971,520 bytes, 40% of the file. Algorithm 1 gives the loop.");
algo("Algorithm 1. Offset auditor, one poll cycle.", [
  "input : pods root P, shipper offset store D, poll interval Δ",
  "state : per inode i: last size s[i], last offset o[i], seen_by_shipper[i]",
  "present <- scan(P)                       // inotify events plus timed rescan",
  "for each (i, size) in present: s[i] <- max(s[i], size)",
  "rows <- read_offsets(D)                  // skip the cycle if D is locked",
  "for each (i, off) in rows: o[i] <- max(o[i], off); seen_by_shipper[i] <- true",
  "for each tracked i not in present and not in rows:",
  "    lost <- max(0, s[i] - o[i])          // o[i] = 0 if the shipper never saw i",
  "    emit(i, pod, container, s[i], o[i], lost, never_opened = not seen_by_shipper[i])",
  "    forget(i)",
]);
h2("7.2. What it sees that shipper metrics do not");
p("The auditor watches the directory, not the shipper's file list, so it records files the shipper never opened: intermediate files skipped between polls, and files removed with an evicted pod before the shipper reached them. In the polling-collector experiments (Section 10.4) every lost byte came from files the shipper never opened. The auditor measured them exactly, while a counter inside the shipper, such as the one proposed in Fluent Bit pull request #12361, would have reported zero [@fbpr12361]. The auditor also works with any shipper whose offset store it can read, and its counters live in a separate process, so they survive a shipper restart.");
h2("7.3. Error sources");
p("Fluent Bit deletes a file's row when it closes the file [@fbsql]. The auditor therefore sees the offset from its last poll before the deletion, which can be stale by up to one poll interval of reading. The auditor over-counts by at most about the shipper's read rate *R* times the poll interval Δ for each abandoned file:");
eq("eq6.png", 4, 0.3);
p("where *A* is the set of files abandoned with unread bytes. Section 10.6 measures this error between 0.5% and 24% across poll intervals of 20 ms to 2 s. The count is in bytes, not lines; dividing by the stream's mean on-disk line size gives an estimate of lines. Inode reuse (Section 3.3) makes inode alone an unsafe key. With escrow enabled, the escrow link pins the inode for as long as the auditor tracks it, so reuse cannot happen. In audit-only mode the prototype keys a file by inode, directory, and order of first sight, and treats a size decrease as a new file. Finally, a file created and removed between two rescans is invisible if inotify also dropped its events; the rescan interval must stay shorter than one rotation interval.");

h1("8. Log escrow");
h2("8.1. Design");
p("Log escrow keeps rotated files readable until the data has been read. On first sight of any container log file, the agent creates a hard link to it in `/var/log/escrow`. The kubelet can then rename, compress, and delete its own names as usual; the inode and its data survive through the escrow link. A hard link cannot cross filesystems, so the escrow directory must sit on the same filesystem as `/var/log/pods` [@link2]. Kernels with `fs.protected_hardlinks` enabled allow the link only to the file's owner or a process with the right capability, so the agent runs as root or with `CAP_FOWNER` [@procsysfs].");
p("The release rule uses the auditor's view. While a file is still the active `0.log`, or the shipper still has a row for it, escrow waits. Once neither holds, escrow compares the file's size with the last saved offset. If the shipper finished, escrow removes its link. If bytes remain, or the shipper never opened the file, escrow copies the unread range into a drain directory that the shipper tails as a separate input, and then removes the link. This adds no new network path: recovered lines travel through the same shipper with a distinct tag. Algorithm 2 gives the rule.");
algo("Algorithm 2. Log escrow, one cycle (runs after Algorithm 1).", [
  "for each log file i present under P and not yet linked:",
  "    link(path(i), escrow_dir/<pod>__<container>__<i>.log)",
  "for each linked i:",
  "    if i is the active 0.log or i in rows: continue      // still in use",
  "    start <- o[i] if seen_by_shipper[i] else 0",
  "    if start >= size(i): unlink(escrow link of i)        // shipper finished",
  "    else: queue drain of [start, size(i)) into drain_dir, then unlink",
  "while bytes held (links with st_nlink = 1) > cap:",
  "    release the oldest queued file; count its unread bytes as overflow loss",
]);
h2("8.2. Costs");
p("Held files count against the node's disk. They also sit outside the pod's directory, so the kubelet's per-pod ephemeral-storage accounting does not see them, while its node-wide `nodefs.available` check does. Escrow can therefore push a node toward the eviction it was meant to survive. The cap must be sized against the node's free space above the eviction threshold, not against total disk. A cap smaller than a single rotated file cannot hold that file at all, because a hard link keeps the whole inode (Section 10.8).");
p("Escrow defers loss; it does not add read capacity. If the node writes faster than the shipper and the escrow drain can read together, the held backlog grows until the cap releases it. Escrow keeps up only when its drain rate *R_e* satisfies");
eq("eq7.png", 5, 0.26);
p("where *R* is the shipper's read rate. In the emulator, with *R_e* at 16 MB/s and a write rate 32 MB/s above the shipper's, escrow held up to 3.07 GB before draining it. When no node component can keep up, the remaining options are more read capacity or fewer bytes per event; the companion paper on source-side log encoding studies the second [@steno].");
p("Escrow starts its drain at the last offset it saw, which may lag the shipper's true position by one poll interval. The overlap arrives twice. At a 100 ms poll this produced about 0.6% duplicate lines. The watcher removes them by keying on (pod UID, restart count, container, sequence number). Escrowed files hold the same content as the originals, including any personal data, so the same retention and access rules apply. For audit logs, escrow is the difference between a gap with a cause and no gap at all, up to the cap.");
p("Two refinements are left for later work. Punching holes in the already-read prefix of an escrowed file would reduce the bytes held, but the kubelet's compression step reads the whole file, so a hole punched before compression would put zeros into the `.gz` copy. And a lease interface in the kubelet or the runtime, letting an agent delay cleanup of a file until it acknowledges reading it, would make escrow unnecessary; escrow is an out-of-tree approximation of that interface.");

h1("9. Direct export");
p("For logs a team cannot afford to lose, the application can skip the file. An OpenTelemetry SDK exports records to a collector over the network, so rotation, eviction of the log directory, and the shipper race never see them (Fig. 1, dashed path). The loss moves into the SDK's batching processor. In the JavaScript and Rust SDKs its defaults are a queue of 2,048 records, batches of 512, a 1 s export delay, and a 30 s export timeout, and records are dropped when the queue is full [@otelblrp][@otelrust]. If the collector is unavailable for *d* seconds and the application emits *r* records per second, a simple model predicts");
eq("eq8.png", 6, 0.28);
p("dropped records for outages shorter than the export timeout *T_exp*, where *Q* is the queue size and *b* the batch in flight. Section 10.9 checks this against a discrete simulation. The sequence markers stay useful on this path: every drop appears as an internal gap, because later records keep arriving once the collector returns.");

h1("10. Evaluation");
h2("10.1. Method");
p("The evaluation uses a node emulator (`sim/nodesim.py`) that performs real file operations on an ext4 scratch directory (write, rename, unlink, hard link, and reads through open descriptors) while advancing a simulated clock in 20 ms steps. Each run is deterministic for a given seed. Six actors share the directory. A writer appends 256-byte CRI-formatted lines, each carrying a sequence number. A kubelet actor performs the rotation sequence of Section 2.1, ported from the kubelet source; compression is modeled as removal of the uncompressed name, and the gzip payload is not written. A shipper actor tails the active file, keeps rotated files open for `Rotate_Wait`, reads at a rate *R* with ±20% jitter per step, and keeps offsets in an SQLite table with the same shape as Fluent Bit's, deleting a row when it closes the file. It has two discovery profiles: an event-driven profile that reopens the active path within 100 ms of a rotation, as Fluent Bit's rotation handler does [@fbtailfile], and a polling profile that notices rotations and new files only every 10 s. The auditor and escrow actors implement Algorithms 1 and 2. A backend actor counts deliveries per sequence number, so every loss, gap, and duplicate is computed from the bytes delivered.");
p("Byte quantities are divided by 16 to fit the runs on one machine; times are not scaled. Loss depends on ratios such as *W*/*R* and *S*/*W*, which the scaling keeps fixed, and all results are reported as unscaled equivalents. Table 4 lists the defaults. Each condition ran with five seeds, and the tables report means; standard deviations of the loss ratio were below 0.7 percentage points in every condition. The full suite takes about 3.5 minutes on one vCPU (Python 3.12, Intel Xeon at 2.10 GHz).");
table("Table 4. Emulator defaults (unscaled equivalents).",
  ["Parameter", "Value", "Parameter", "Value"],
  [
    ["containerLogMaxSize", "10 MiB", "Shipper read rate R", "32 MB/s"],
    ["containerLogMaxFiles", "5", "Rotate_Wait", "5 s"],
    ["containerLogMonitorInterval", "10 s", "Offset commit", "every read"],
    ["Writer active time", "90 s", "Auditor offset poll", "100 ms"],
    ["Line size on disk", "256 B", "Escrow drain rate R_e", "16 MB/s"],
    ["High-water gauge scrape", "15 s", "Seeds per condition", "5"],
  ], [1.95, 1.3, 1.95, 1.3]);
p("The emulator omits several things a real node has: CPU contention between the kubelet, runtime, and shipper; the time gzip takes; the kubelet's worker queue across many containers; containerd's handling of writes during a rotation; network backpressure beyond the modeled outages; and more than one container per node. Its shipper is a model, not Fluent Bit. The results show how the mechanisms behave under stated assumptions; Section 11 describes how to confirm them on a cluster.");
h2("10.2. Loss against write rate");
p("With the event-driven shipper reading 32 MB/s, no line was lost at write rates up to 32 MB/s (Fig. 3a, Table 5). Above that rate the shipper's backlog grows without limit, rotated files start to exceed `Rotate_Wait`, and loss follows the throughput bound");
eq("eq3.png", 7, 0.4);
p("approached from below because the run ends. Over 90 s, 14.4%, 28.4%, and 46.1% of lines were lost at 40, 48, and 64 MB/s, against bounds of 20%, 33%, and 50%. With escrow enabled and no cap, no line was lost at any rate.");
fig("fig3_rate_and_onset.png", "Fig. 3. (a) Lines lost against write rate for a shipper reading 32 MB/s, with and without escrow; the dotted line is the bound 1 - R/W. (b) Time of the first abandoned file against Rotate_Wait at W = 48 MB/s over a 300 s run; the dotted line is Eq. (8). Error bars show one standard deviation over five seeds.");
table("Table 5. Write-rate sweep (event-driven shipper, R = 32 MB/s, 90 s, mean of 5 seeds).",
  ["W (MB/s)", "Lines lost", "First loss (s)", "Largest 0.log (MB)", "Gap check recall", "With gauge", "Auditor over-count", "Lost with escrow", "Escrow held (MB)", "Duplicates"],
  [
    ["8 to 32", "0%", "none", "80 to 319", "n/a", "n/a", "n/a", "0%", "0", "0"],
    ["40", "14.4%", "29.4", "399", "99.0%", "100%", "+4.0%", "0%", "400", "0.58%"],
    ["48", "28.4%", "19.4", "479", "97.0%", "100%", "+2.1%", "0%", "480", "0.59%"],
    ["64", "46.1%", "15.4", "639", "95.8%", "100%", "+1.0%", "0%", "3,072", "0.46%"],
  ], [0.62, 0.6, 0.62, 0.7, 0.7, 0.55, 0.72, 0.62, 0.62, 0.65],
  "Gap check recall is the share of lost lines inside a gap that a later line reveals. The rest were tail losses, which the 15 s gauge found in every case here. Duplicates are a share of lines written.");
p("The largest active file grew in line with Eq. (1): at 48 MB/s it reached 479 MB before the 10 s monitor tick rotated it. At write rates above *R*, 1% to 4% of lost lines sat after the last line delivered, so a gap check alone missed them; the gauge found them all. The auditor over-counted lost bytes by 1.0% to 4.0%, within the bound of Eq. (4).");
h2("10.3. Rotate_Wait buys time, not capacity");
p("At 48 MB/s over a 300 s run, raising `Rotate_Wait` delayed the first abandoned file linearly (Fig. 3b). A least-squares constant of 1.5 s fits all six settings to within 0.1 s:");
eq("eq4.png", 8, 0.42);
p("where *w* is `Rotate_Wait`. The first loss came at 7.4 s with a 1 s wait and at 184.4 s with a 60 s wait. Loss over the 300 s run fell only from 32.8% to 19.6%, because once loss begins every later rotated file is abandoned too. A longer wait is useful for bursts shorter than the delay it buys. For sustained overload it postpones the loss.");
h2("10.4. Monitor interval and a polling shipper");
p("At 16 MB/s, half the shipper's read rate, the event-driven shipper lost nothing at any monitor interval. The polling shipper, which notices new files every 10 s, lost nothing at the default 10 s interval and 50.0%, 78.7%, and 89.3% of lines at 5, 2, and 1 s (Fig. 4). Every lost byte came from files the shipper never opened: on average 9, 35, and 80 of them per run. The auditor measured these losses with zero error, because an unopened file counts in full and its size is exact. Escrow recovered every line, holding at most 80 MB.");
fig("fig4_poll_monitor.png", "Fig. 4. Lines lost at W = 16 MB/s against containerLogMonitorInterval, for an event-driven shipper and a shipper that polls for new files every 10 s. Shortening the interval reduces rotation lag and, with a polling shipper, causes rotations the shipper never sees.");
h2("10.5. Real-time test of the agent");
p("The prototype agent (`agent/logguard.py`) ran as a separate process against a real-time node stand-in (`agent/smoke_test.py`): a writer at 2 MB/s for 10 s, a rotator following the kubelet sequence every second with a 1 MB limit and three files, and a tail shipper reading 1 MB/s with a 1 s `Rotate_Wait` and a Fluent Bit-shaped offset table. The pod directory was removed at the end, as on eviction. In three runs, 76,596 lines were written and between 35,919 and 35,958 (46.9%) never reached the shipper's output. With escrow, every one reached the backend through the drain directory. The auditor over-counted unread bytes by 1.4% to 1.8%, and escrow delivered between 507 and 663 duplicate lines. This test used inotify, a real SQLite database read by a second process, and real hard links; it did not use a kubelet or Fluent Bit.");
h2("10.6. Auditor accuracy against poll interval");
p("At 48 MB/s, the auditor's over-count grew with its offset poll interval: 0.5% at 20 ms, 2.1% at 100 ms, 7.7% at 500 ms, 9.9% at 1 s, and 23.6% at 2 s (Fig. 5). Escrow duplicates grew the same way, from 0.13% to 6.6% of lines written, because both errors come from the stale offset. Every measurement stayed within Eq. (4). A 100 ms poll kept both errors near 2% and 0.6% here.");
fig("fig5_auditor_poll.png", "Fig. 5. Auditor over-count of lost bytes against the interval at which it polls the shipper's offsets (W = 48 MB/s, five seeds). The error comes from the shipper deleting its offset row when it closes a file.", 6.0);
h2("10.7. Eviction during shipper backpressure");
p("A container writing 8 MB/s was evicted while its shipper was paused by a downstream outage from 20 s to 70 s. Evictions at 30, 40, 50, and 60 s lost 33%, 50%, 60%, and 67% of the lines written. All of them were tail losses, so the gap check found none (Fig. 6). The 15 s gauge found 0%, 50%, 83%, and 63%, depending on how long before the eviction its last scrape fell. The auditor measured between 99.4% and 99.9% of the lost bytes; the small shortfall is partial lines. Escrow, holding about 87 MB, recovered every line.");
fig("fig6_eviction_detection.png", "Fig. 6. Share of eviction losses each detector found, by eviction time. Gap checks find nothing because no line follows the loss. The gauge finds losses up to its last scrape. The auditor measures bytes, independent of the application.");
h2("10.8. Escrow cap");
p("At 48 MB/s each abandoned rotated file held about 480 MB. With a cap of 500 MB or more, escrow lost nothing. With a 250 MB cap it could not hold any abandoned file, released 611 MB, and loss fell only from 28.4% to 13.9%, the part recovered from files still named on disk. A cap below the size of one rotated file makes escrow nearly useless, and the rotated file size under rotation lag is W times the monitor interval, not `containerLogMaxSize`.");
h2("10.9. Direct export");
table("Table 6. Records dropped by the SDK batch processor during a collector outage (90 s run).",
  ["Records/s", "1 s outage", "2 s", "5 s", "10 s", "40 s"],
  [
    ["1,000", "0", "0", "2,478", "7,478", "37,478 (41.6%)"],
    ["5,000", "2,630", "7,630", "22,630", "47,630", "197,630 (43.9%)"],
    ["20,000", "17,688", "37,688", "97,688", "197,688", "797,688 (44.3%)"],
  ], [1.2, 1.0, 1.0, 1.0, 1.0, 1.3],
  "Model: queue 2,048, batch 512, 1 s delay, 30 s export timeout, 5 ms export latency. The 40 s outage exceeds the timeout, so one in-flight batch of 512 also times out.");
p("For outages shorter than the export timeout, drops matched Eq. (6) to within 250 records, the extra coming from the export latency. A service logging 1,000 records per second rides out a 2 s outage; one logging 20,000 per second loses 17,688 records in 1 s. Direct export removes the node mechanisms but not loss, and the markers still measure it.");

h1("11. Cluster evaluation plan (not yet run)");
p("The repository includes a kind-based kit (`cluster/`) with a sequence-marked log generator that exports the gauge, Fluent Bit configured with a readable offset database, the logguard DaemonSet, an HTTP receiver standing in for the backend, and an analysis script. It has been syntax-checked and not run. Each mechanism has a reproduction path. For rotation lag, a StatefulSet ramps from 1 MB/s to 60 MB/s with `containerLogMaxSize` at 50Mi, alongside co-located heavy loggers and `stress-ng` load, matching issues #110630 and #134324 [@i110630][@i134324]. For eviction, a DaemonSet fills nodefs while the target logs at a known rate. For the shipper race, a burst generator reproduces the timing in issue #10414 across many trials, since the race is timing-sensitive [@fb10414]. Prometheus records `kubelet_evictions`, node conditions, and the gauge throughout.");
p("The success criteria are fixed now, before the data exists:");
list([
  "Gap-detection recall above 95% of induced lost lines, counting the gauge. Sequence numbers make internal gaps mechanically visible, so lower recall would indicate a watcher defect.",
  "Classification accuracy above 90% against the induced cause in each run.",
  "False-positive rate below 1% during a 72-hour soak without induced loss, with ordinary restarts and deploys.",
  "Auditor-measured lost bytes within 5% of the loss computed from missing sequence numbers times mean line size.",
  "No lost lines with escrow below its cap, and duplicates below 1% of lines at a 100 ms poll.",
  "Every condition reported with R_m and the median and 95th-percentile gap size. A statement such as \"at 8 MB/s with four co-located heavy loggers, R_m was 0.03, the median gap 40 lines, and the 95th-percentile gap 900 lines\" is a citable result; \"it can happen\" is not.",
]);
p("The same runs record the agent's CPU and memory use and the rotation delay from kubelet logs and inode changes. If a cluster result departs from the emulator by more than the stated tolerances, the departure identifies what the emulator leaves out, which is itself a finding.");

h1("12. Limitations");
p("The numbers in Section 10 come from an emulator and a real-time stand-in, not from a kubelet and a production shipper. The shipper model decides much of the outcome: a shipper that reads rotated files in a different order, commits offsets less often, or discovers files differently will lose different amounts. The emulator also has one container per node and no CPU contention, which is where rotation lag is worst in practice.");
p("Sequence markers require a change to each service's logging path, which is a real adoption barrier. The auditor and escrow need no application change but need a privileged DaemonSet with write access to the host's `/var/log`, which some platforms forbid. The prototype reads only Fluent Bit's offset store; Filebeat's registry and the OpenTelemetry Collector's file-log checkpoints need their own readers. The auditor reports bytes, and converting them to lines is an estimate.");
p("Classification is coarse. On a multi-tenant node a noisy neighbor can raise write volume and disk pressure together, so two causes fit one gap; the rules report such cases as ambiguous rather than guessing.");
p("If the lost logs are audit logs, a gap may be a compliance finding that must be disclosed. Detection after the fact does not restore data. It tells an operator, with a time and a cause, what is missing, which matters for the incident report even when it cannot help during the incident. Escrow is the only part of this work that prevents loss, and only up to its cap.");

h1("13. Recommendations");
p("In the short term, treat the kubelet's rotation settings and the shipper's discovery method as one configuration. Do not shorten `containerLogMonitorInterval` without confirming the shipper reacts to rotation events rather than polling. Size `Rotate_Wait` for bursts, and alert on shipper byte counters where they exist. Set ephemeral-storage requests and limits per pod.");
p("In the medium term, run the offset auditor in measure-only mode on a few nodes to get a baseline lost-bytes figure per node and per workload. It needs no application change. Add sequence markers and the gauge to the services where a logging gap during an incident costs time or money, starting with one service. Enable escrow for audit-log workloads, with a cap sized against free space above the eviction threshold and larger than the largest rotated file those workloads produce.");
p("In the long term, three upstream changes would replace inference with signals: a kubelet event or metric at each rotation and at each removal of a pod's log directory; byte-level abandoned-data counters in shippers, as Fluent Bit pull request #12361 and Vector issue #24675 propose [@fbpr12361][@vec24675]; and a lease interface that lets a log agent delay cleanup of a rotated file until it acknowledges reading it, which escrow approximates from outside.");

h1("14. Conclusion");
p("Kubernetes loses container logs through three mechanisms that tell no one: rotation lag, which inflates files; eviction, which removes a pod's logs with the pod; and the shipper race, in which a shipper abandons a file it could not drain or never opens one it did not see. The kubelet's rotation code gives a shipper one rotation interval to open a rotated file by name. On the emulator, loss began exactly when the write rate passed the shipper's read rate, a longer `Rotate_Wait` only delayed it, and shortening the monitor interval cost a polling shipper up to 89% of lines.");
p("Sequence markers detect internal gaps; a high-water gauge is needed for tail gaps, which gap checks missed entirely after eviction. The offset auditor measures lost bytes on the node without application changes, including files no shipper opened, within a few percent at a 100 ms poll. Log escrow prevented every loss in the emulator up to its cap, at a cost in held disk and a small share of duplicates. Whether these numbers hold on a live kubelet is the question the published cluster kit is built to answer. A companion paper studies the other lever, reducing the bytes each event costs at the source [@steno].");

h1("Data and code availability");
p("The emulator, experiment runner, raw results (CSV), figure scripts, agent prototype, real-time test, atomic benchmark, and cluster kit are available at https://github.com/prasad-m-k/k8s-silent-log-loss. Running `python experiments/run_all.py` followed by `python analysis/make_figures.py` regenerates every number and figure in Section 10.");
h1("Declaration of generative AI and AI-assisted technologies");
p("During the preparation of this work the author used Claude (Anthropic) to assist with literature search, drafting and editing text, and writing the emulator, agent, and analysis scripts. After using this tool, the author reviewed and edited the content as needed and takes full responsibility for the content of this preprint.");
h1("Declaration of competing interest");
p("The author declares no known competing financial interests or personal relationships that could have appeared to influence the work reported in this paper. The work was done independently and is not affiliated with the author's employer.");
h1("Funding");
p("This research did not receive any specific grant from funding agencies in the public, commercial, or not-for-profit sectors.");

const REFS = {
  k8slog: 'Kubernetes Documentation, "Logging architecture." [Online]. Available: https://kubernetes.io/docs/concepts/cluster-administration/logging/ (accessed Sep. 24, 2026).',
  kubeletcfg: 'Kubernetes Documentation, "Kubelet configuration (v1beta1)." [Online]. Available: https://kubernetes.io/docs/reference/config-api/kubelet-config.v1beta1/ (accessed Sep. 24, 2026).',
  pr114301: 'kubernetes/kubernetes, "kubelet: enable configurable rotation duration and parallel rotate," GitHub pull request #114301. [Online]. Available: https://github.com/kubernetes/kubernetes/pull/114301',
  bottlerocket: 'Bottlerocket Project, "Kubernetes settings: container-log-monitor-interval (Kubernetes 1.30 and later)." [Online]. Available: https://bottlerocket.dev/en/os/1.54.x/api/settings/kubernetes/ (accessed Sep. 24, 2026).',
  clm: 'kubernetes/kubernetes, "pkg/kubelet/logs/container_log_manager.go," master branch. [Online]. Available: https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/logs/container_log_manager.go (accessed Sep. 24, 2026).',
  i110630: 'kubernetes/kubernetes, "Kubelet does not respect container-log-max-size on time, during heavy log writes from container," GitHub issue #110630, 2022.',
  i134324: 'kubernetes/kubernetes, "High-throughput container logs cause kubelet log rotation to hang, leading to unbounded 0.log growth (v1.27+ timestamp-based rotation)," GitHub issue #134324, 2025.',
  k8sevict: 'Kubernetes Documentation, "Node-pressure eviction." [Online]. Available: https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/ (accessed Sep. 24, 2026).',
  fb2110: 'fluent/fluent-bit, "Tail input plugin not fetching all logs when files are rotated quickly under heavy load," GitHub issue #2110, 2020.',
  fb10414: 'fluent/fluent-bit, "Rotate_Wait should not abandon log file if it still has pending_bytes," GitHub issue #10414, 2025.',
  fd465: 'fluent/fluentd-kubernetes-daemonset, "Fluentd misses log file when >1 app log rotation happens back to back," GitHub issue #465, 2020.',
  i129975: 'kubernetes/kubernetes, "Potential for dropped logs when multiple log rotations occur before log collector (Fluentd) detects rotation," GitHub issue #129975, Feb. 2025.',
  cribl: 'Cribl, "Kubernetes Logs Source," Cribl Edge documentation. [Online]. Available: https://docs.cribl.io/edge/sources-kubernetes-logs (accessed Sep. 24, 2026).',
  fbtailfile: 'fluent/fluent-bit, "plugins/in_tail/tail_file.c (flb_tail_file_rotated)," master branch. [Online]. Available: https://github.com/fluent/fluent-bit/blob/master/plugins/in_tail/tail_file.c (accessed Sep. 24, 2026).',
  fbtail: 'Fluent Bit Documentation, "Tail input plugin." [Online]. Available: https://docs.fluentbit.io/manual/data-pipeline/inputs/tail (accessed Sep. 24, 2026).',
  fbsql: 'fluent/fluent-bit, "plugins/in_tail/tail_sql.h," v1.9.9. [Online]. Available: https://github.com/fluent/fluent-bit/blob/v1.9.9/plugins/in_tail/tail_sql.h',
  vec24675: 'vectordotdev/vector, "Add files_unwatched_bytes_unread_total metric to track unread bytes when files are unwatched," GitHub issue #24675, Feb. 2026.',
  fbpr12361: 'G. Cao, "in_tail: add processed and abandoned raw byte metrics," fluent/fluent-bit GitHub pull request #12361, Sep. 2026 (open at the time of writing).',
  otelcol: 'OpenTelemetry Authors, "Internal telemetry," OpenTelemetry Collector documentation. [Online]. Available: https://opentelemetry.io/docs/collector/internal-telemetry/ (accessed Sep. 24, 2026).',
  beats2395: 'elastic/beats, "Filebeat not closing deleted/rotated files (when output broken)," GitHub issue #2395, Aug. 2016.',
  vecviaq: 'ViaQ/vector, "rotate_wait_ms for the file and kubernetes_logs sources," GitHub pull request #154. [Online]. Available: https://github.com/ViaQ/vector/pull/154',
  logtail: 'Alibaba Cloud, "Logtail," Simple Log Service documentation. [Online]. Available: https://help.aliyun.com/en/sls/logtail (accessed Sep. 24, 2026).',
  vmbench: 'VictoriaMetrics, "Benchmarking Kubernetes log collectors: vlagent, Vector, Fluent Bit, OpenTelemetry Collector, and more," Mar. 2026. [Online]. Available: https://victoriametrics.com/blog/log-collectors-benchmark-2026/',
  kafka: 'J. Kreps, N. Narkhede, and J. Rao, "Kafka: A distributed messaging system for log processing," in Proc. NetDB Workshop, Athens, Greece, 2011.',
  crosby: 'S. A. Crosby and D. S. Wallach, "Efficient data structures for tamper-evident logging," in Proc. 18th USENIX Security Symposium, Montreal, Canada, 2009, pp. 317-334.',
  inotify: 'Linux man-pages project, "inotify(7): monitoring filesystem events." [Online]. Available: https://man7.org/linux/man-pages/man7/inotify.7.html',
  link2: 'Linux man-pages project, "link(2): make a new name for a file." [Online]. Available: https://man7.org/linux/man-pages/man2/link.2.html',
  procsysfs: 'Linux kernel documentation, "Documentation for /proc/sys/fs/: protected_hardlinks." [Online]. Available: https://docs.kernel.org/admin-guide/sysctl/fs.html',
  otelblrp: 'OpenTelemetry Authors, "BatchLogRecordProcessorOptions," OpenTelemetry JavaScript SDK API documentation. [Online]. Available: https://open-telemetry.github.io/opentelemetry-js/interfaces/_opentelemetry_sdk-logs.BatchLogRecordProcessorOptions.html',
  otelrust: 'OpenTelemetry Authors, "BatchConfigBuilder," opentelemetry_sdk Rust crate documentation (OTEL_BLRP_MAX_QUEUE_SIZE, OTEL_BLRP_SCHEDULE_DELAY).',
  steno: 'Prasad MK, "Log stenography: Shrinking container log bytes at the source to stay inside Kubernetes rotation and eviction limits," manuscript in preparation, 2026.',
};

const ABSTRACT = "Kubernetes nodes rotate, compress, and delete container log files on their own schedule, and none of these actions reaches the application that wrote the lines. This paper documents three mechanisms that remove container logs without an error: rotation lag under sustained writes, disk-pressure eviction, and a race between kubelet rotation and the log shipper. It specifies a detection layer built on application-level sequence markers and ordered classification rules, and adds three node-side parts: a high-water gauge that exposes losses at the end of a stream, an offset auditor that measures lost bytes without application changes by comparing each log file's final size with the shipper's saved offset, and log escrow, which hard-links log files so they outlive the kubelet's cleanup until their data has been read. A node emulator that ports the kubelet's rotation sequence and performs real file operations measured each part. With a shipper reading 32 MB/s, loss began once writes passed that rate and reached 14%, 28%, and 46% of lines at 40, 48, and 64 MB/s. A longer Rotate_Wait delayed the first loss linearly but did not prevent it. Shortening the kubelet's monitor interval cost a polling shipper 50% to 89% of lines. After eviction, gap checks found none of the lost lines, while the auditor measured over 99% of the lost bytes. Escrow prevented all loss in uncapped runs, at the cost of up to 3 GB of held disk and about 0.6% duplicate lines. The code, raw results, and a cluster evaluation kit are published.";
const KEYWORDS = "Kubernetes, container logging, log rotation, data loss detection, log shipper, observability, sequence numbers, Fluent Bit";

module.exports = { B, REFS, ABSTRACT, KEYWORDS };

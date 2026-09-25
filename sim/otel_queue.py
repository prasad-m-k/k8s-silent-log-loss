"""
otel_queue.py - loss model for direct export through an OpenTelemetry SDK.

Direct export (App + OTel SDK -> Collector) skips 0.log, so rotation,
eviction of the log directory, and the shipper race cannot touch those
records. The SDK's batching log record processor keeps its own bounded
queue instead. Default settings in the JS, Rust and PHP SDKs are
maxQueueSize=2048, maxExportBatchSize=512, scheduledDelayMillis=1000,
exportTimeoutMillis=30000; records are dropped when the queue is full.

The model: records arrive at a constant rate. One export runs at a time.
An export takes `latency` seconds when the collector is up. While the
collector is down, an export blocks until the collector returns or the
export timeout expires; a timed-out batch is dropped. Every record
carries a sequence number, so the receiving side can count gaps.
"""

from dataclasses import dataclass


@dataclass
class OtelConfig:
    rate: float = 5000.0          # records per second
    duration: float = 60.0
    outage: tuple = (20.0, 25.0)  # collector unavailable [start, end)
    max_queue: int = 2048
    batch: int = 512
    delay: float = 1.0
    export_timeout: float = 30.0
    latency: float = 0.005
    dt: float = 0.001


def run(cfg: OtelConfig) -> dict:
    q = 0                 # records waiting
    seq_emitted = 0
    dropped_queue = 0
    dropped_timeout = 0
    delivered = 0
    busy_until = 0.0
    in_flight = 0
    in_flight_start = 0.0
    next_timer = cfg.delay
    acc = 0.0
    t = 0.0
    steps = int(cfg.duration / cfg.dt)
    a, b = cfg.outage
    for i in range(steps):
        t = i * cfg.dt
        acc += cfg.rate * cfg.dt
        n = int(acc)
        acc -= n
        seq_emitted += n
        room = cfg.max_queue - q
        take = min(n, room)
        q += take
        dropped_queue += n - take
        # finish an export
        if in_flight and t >= busy_until:
            down_at_start = a <= in_flight_start < b
            if down_at_start and (busy_until - in_flight_start) >= cfg.export_timeout:
                dropped_timeout += in_flight
            else:
                delivered += in_flight
            in_flight = 0
        # start an export
        if not in_flight and q > 0 and (q >= cfg.batch or t >= next_timer):
            k = min(cfg.batch, q)
            q -= k
            in_flight = k
            in_flight_start = t
            if a <= t < b:
                busy_until = t + min(b - t, cfg.export_timeout) + cfg.latency
            else:
                busy_until = t + cfg.latency
            next_timer = t + cfg.delay
    lost = dropped_queue + dropped_timeout
    return dict(rate=cfg.rate, outage_s=b - a, emitted=seq_emitted,
                dropped_queue=dropped_queue, dropped_timeout=dropped_timeout,
                lost=lost, loss_ratio=lost / seq_emitted if seq_emitted else 0.0,
                predicted_lost=max(0.0, cfg.rate * (b - a) - cfg.max_queue - cfg.batch)
                if b - a < cfg.export_timeout else None)


if __name__ == "__main__":
    for o in (0, 1, 2, 5, 10):
        print(run(OtelConfig(outage=(20.0, 20.0 + o))))

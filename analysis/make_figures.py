"""
make_figures.py - builds every data figure in the paper from results/*.csv.

Usage: python analysis/make_figures.py
Writes PNGs (300 DPI) to figures/. Palette: Okabe-Ito for data, markers and
line styles as well as color so figures survive grayscale printing.
"""

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                       # noqa: E402
import pandas as pd                      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")
FIG = os.path.join(ROOT, "figures")
os.makedirs(FIG, exist_ok=True)

OI = dict(orange="#E69F00", sky="#56B4E9", green="#009E73", yellow="#F0E442",
          blue="#0072B2", vermil="#D55E00", rose="#CC79A7", black="#000000")
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8.5, "axes.titlesize": 9,
                     "axes.labelsize": 8.5, "legend.fontsize": 8, "xtick.labelsize": 8,
                     "ytick.labelsize": 8})


def new(w=6.5, h=2.8, ncols=1):
    fig, ax = plt.subplots(1, ncols, figsize=(w, h))
    fig.patch.set_facecolor("white")
    for a in np.atleast_1d(ax):
        a.set_facecolor("white")
        a.grid(True, color="#DDDDDD", linewidth=0.6)
        a.set_axisbelow(True)
    return fig, ax


def save(fig, name):
    fig.tight_layout(pad=0.4)
    fig.savefig(os.path.join(FIG, name), dpi=300, facecolor="white")
    plt.close(fig)
    print("wrote", name)


def agg(df, by, col):
    g = df.groupby(by)[col]
    return g.mean(), g.std().fillna(0)


def fig_rate_and_wait():
    e1 = pd.read_csv(os.path.join(RES, "e1_rate_sweep.csv"))
    e2 = pd.read_csv(os.path.join(RES, "e2_rotate_wait.csv"))
    fig, (a, b) = new(ncols=2, h=2.7)
    m, s = agg(e1, "W_MBps", "loss_ratio")
    R = e1["R_MBps"].iloc[0]
    w = np.linspace(8, 64, 200)
    a.plot(w, np.clip(1 - R / w, 0, None) * 100, color=OI["black"], linestyle=":", linewidth=1.2,
           label="bound 1 - R/W")
    a.errorbar(m.index, m * 100, yerr=s * 100, color=OI["vermil"], marker="o", markersize=4,
               linewidth=1.3, capsize=2, label="shipper only")
    me, se = agg(e1, "W_MBps", "escrow_loss_ratio")
    a.errorbar(me.index, me * 100, yerr=se * 100, color=OI["blue"], marker="s", markersize=4,
               linestyle="--", linewidth=1.3, capsize=2, label="shipper + escrow")
    a.axvline(R, color="#999999", linewidth=0.8)
    a.text(R + 0.8, 24, "R = %d MB/s" % R, fontsize=7.5, color="#555555")
    a.set_xlabel("Write rate W (MB/s)")
    a.set_ylabel("Lines lost (%)")
    a.set_title("(a) Loss vs write rate, 90 s run")
    a.legend(loc="upper left", frameon=False)
    m2, s2 = agg(e2, "rotate_wait", "first_abandon_s")
    b.errorbar(m2.index, m2, yerr=s2, color=OI["vermil"], marker="o", markersize=5,
               markerfacecolor="white", linestyle="none", capsize=2, label="measured (mean of 5 seeds)", zorder=3)
    frac = 1 - e2["R_MBps"].iloc[0] / e2["W_MBps"].iloc[0]
    xs = np.array(sorted(m2.index))
    c = float(np.mean(m2.values * frac - xs))
    b.plot(xs, (xs + c) / frac, color=OI["black"], linestyle=":", linewidth=1.2,
           label="(w + %.1f s) / (1 - R/W)" % c)
    b.set_xlabel("Rotate_Wait w (s)")
    b.set_ylabel("First abandoned file (s)")
    b.set_title("(b) Loss onset at W = 48, R = 32 MB/s")
    b.legend(loc="upper left", frameon=False)
    save(fig, "fig3_rate_and_onset.png")


def fig_poll():
    e3 = pd.read_csv(os.path.join(RES, "e3_poll_monitor.csv"))
    fig, ax = new(h=2.5)
    mons = sorted(e3["monitor_interval"].unique(), reverse=True)
    x = np.arange(len(mons))
    wdt = 0.36
    for i, (prof, col, hatch) in enumerate([("inotify", OI["blue"], ""), ("poll", OI["orange"], "//")]):
        d = e3[e3.profile == prof].groupby("monitor_interval")["loss_ratio"]
        m, s = d.mean().reindex(mons), d.std().reindex(mons).fillna(0)
        bars = ax.bar(x + (i - 0.5) * wdt, m * 100, wdt, yerr=s * 100, capsize=2, color=col,
                      edgecolor="black", linewidth=0.6, hatch=hatch,
                      label="event-driven shipper" if prof == "inotify" else "polling shipper (10 s refresh)")
        for bx, v in zip(bars, m):
            ax.text(bx.get_x() + bx.get_width() / 2, v * 100 + 2, "%.0f" % (v * 100), ha="center", fontsize=7.5)
    ax.set_xticks(x)
    ax.set_xticklabels(["%g s" % v for v in mons])
    ax.set_xlabel("containerLogMonitorInterval")
    ax.set_ylabel("Lines lost (%)")
    ax.set_ylim(0, 100)
    ax.legend(loc="upper left", frameon=False)
    save(fig, "fig4_poll_monitor.png")


def fig_auditor():
    e4 = pd.read_csv(os.path.join(RES, "e4_auditor_poll.csv"))
    fig, a = new(h=2.5)
    m, s = agg(e4, "audit_interval", "auditor_error_pct")
    a.errorbar(m.index, m, yerr=s, color=OI["vermil"], marker="o", markersize=4, linewidth=1.3,
               capsize=2, label="auditor over-count of lost bytes (%)")
    a.set_xscale("log")
    a.set_xlabel("Offset poll interval (s)")
    a.set_ylabel("Over-count (%)")
    for x, y in zip(m.index, m):
        a.annotate("%.1f" % y, (x, y), textcoords="offset points", xytext=(0, 6), ha="center", fontsize=7.5)
    a.legend(loc="upper left", frameon=False)
    save(fig, "fig5_auditor_poll.png")


def fig_eviction():
    e5 = pd.read_csv(os.path.join(RES, "e5_eviction.csv"))
    fig, ax = new(h=2.5)
    ev = sorted(e5["evict_at"].unique())
    x = np.arange(len(ev))
    wdt = 0.26
    series = [("watcher_recall_gaps_only", "gap check only", OI["vermil"], ""),
              ("watcher_recall_with_hwm", "gap check + 15 s high-water gauge", OI["orange"], "//"),
              (None, "offset auditor (bytes)", OI["blue"], "..")]
    for i, (col, lab, c, h) in enumerate(series):
        if col:
            v = e5.groupby("evict_at")[col].mean().reindex(ev) * 100
        else:
            g = e5.groupby("evict_at")
            v = (g["auditor_lost_MB_equiv"].mean() / g["lost_MB_equiv"].mean()).reindex(ev) * 100
        bars = ax.bar(x + (i - 1) * wdt, v, wdt, color=c, edgecolor="black", linewidth=0.6, hatch=h, label=lab)
        for bx, val in zip(bars, v):
            ax.text(bx.get_x() + bx.get_width() / 2, val + 2, "%.0f" % val, ha="center", fontsize=7.5)
    ax.set_xticks(x)
    ax.set_xticklabels(["evicted at %d s" % e for e in ev])
    ax.set_ylabel("Lost data detected (%)")
    ax.set_ylim(0, 118)
    ax.legend(loc="upper left", frameon=False, ncol=3)
    save(fig, "fig6_eviction_detection.png")


if __name__ == "__main__":
    fig_rate_and_wait()
    fig_poll()
    fig_auditor()
    fig_eviction()

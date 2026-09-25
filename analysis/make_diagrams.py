"""
make_diagrams.py - the two architecture diagrams (Fig. 1 and Fig. 2).
Palette A (Science Journal Minimalist). Usage: python analysis/make_diagrams.py
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIG = os.path.join(ROOT, "figures")
P = dict(primary="#1F4E5B", teal="#4A7C7A", gold="#B8860B", light="#E2E8F0", dark="#1A202C",
         rust="#CA6642")


def box(ax, x, y, w, h, text, fill, tc="white", fs=8, ls="-", lw=0.8, ec=None):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=fill, edgecolor=ec or P["dark"], linewidth=lw, linestyle=ls))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", color=tc, fontsize=fs, linespacing=1.25)


def arrow(ax, x1, y1, x2, y2, text=None, ls="-", color=None, rad=0.0, fs=7, tx=None, ty=None):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=9,
                                 color=color or P["dark"], linewidth=0.9, linestyle=ls,
                                 connectionstyle=f"arc3,rad={rad}"))
    if text:
        ax.text(tx if tx is not None else (x1 + x2) / 2, ty if ty is not None else (y1 + y2) / 2,
                text, fontsize=fs, color=color or P["dark"], ha="center", va="center",
                bbox=dict(facecolor="white", edgecolor="none", pad=0.6))


def fig1():
    fig, ax = plt.subplots(figsize=(6.5, 3.9))
    fig.patch.set_facecolor("white")
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 8.1)
    ax.axis("off")
    ax.add_patch(FancyBboxPatch((0.1, 3.9), 2.5, 3.4, boxstyle="round,pad=0.02", facecolor="white",
                                edgecolor=P["dark"], linewidth=0.7))
    ax.text(1.35, 7.0, "Pod", fontsize=8, ha="center")
    ax.add_patch(FancyBboxPatch((3.0, 0.3), 6.1, 7.0, boxstyle="round,pad=0.02", facecolor=P["light"],
                                edgecolor=P["dark"], linewidth=0.7, linestyle="--"))
    ax.text(6.05, 7.0, "Node layer (not visible to the application)", fontsize=8, ha="center")
    box(ax, 0.35, 5.2, 2.0, 1.4, "Application\nseq marker\n(uid, restart, seq)", P["primary"], fs=7.5)
    box(ax, 0.35, 4.1, 2.0, 0.8, "gauge: last\nseq emitted", P["teal"], fs=7)
    box(ax, 3.3, 5.5, 2.2, 1.1, "Container runtime\nappends CRI lines", P["teal"], fs=7.5)
    box(ax, 6.6, 5.5, 2.3, 1.1, "Kubelet\nrotate, compress,\nremove, evict", P["rust"], fs=7.5)
    box(ax, 3.3, 3.3, 2.2, 1.6, "/var/log/pods/.../\n0.log\n0.log.<ts>\n0.log.<ts>.gz", "white", tc=P["dark"], fs=7.2)
    box(ax, 6.6, 3.3, 2.3, 1.6, "Log shipper\ntail, Rotate_Wait,\noffsets in SQLite", P["primary"], fs=7.5)
    box(ax, 3.3, 0.5, 5.6, 2.2, "", P["gold"], lw=1.1)
    ax.text(6.1, 2.42, "logguard DaemonSet: watches /var/log/pods", color="white", fontsize=7.5,
            ha="center", weight="bold")
    box(ax, 3.5, 0.7, 2.3, 1.4, "Log escrow\nhard-links each file,\ndrains unread tail", "white", tc=P["dark"], fs=7)
    box(ax, 6.4, 0.7, 2.3, 1.4, "Offset auditor\nfinal size minus\nsaved offset", "white", tc=P["dark"], fs=7)
    box(ax, 10.0, 4.6, 2.8, 1.3, "Log backend", P["primary"])
    box(ax, 10.0, 2.5, 2.8, 1.5, "Continuity watcher\ngaps per (uid, restart,\ncontainer), classifier", P["teal"], fs=7.2)
    box(ax, 10.0, 0.5, 2.8, 1.4, "Prometheus: lost bytes,\ngauge, kubelet events,\nnode conditions", "white", tc=P["dark"], fs=7)
    arrow(ax, 2.35, 5.9, 3.3, 6.05, "stdout", tx=2.8, ty=6.3)
    arrow(ax, 4.4, 5.5, 4.4, 4.9, "write", tx=4.8, ty=5.2)
    arrow(ax, 6.6, 5.8, 5.5, 4.6, rad=0.15, color=P["rust"], text="rename, unlink", tx=6.05, ty=5.05)
    arrow(ax, 5.5, 4.1, 6.6, 4.1, "read", ty=4.35)
    arrow(ax, 8.9, 4.4, 10.0, 5.1)
    arrow(ax, 11.4, 4.6, 11.4, 4.0)
    arrow(ax, 4.4, 3.3, 4.4, 2.1, text="hard link", tx=4.0, ty=2.9)
    arrow(ax, 7.75, 3.3, 7.55, 2.1, text="saved offsets", tx=8.35, ty=2.9)
    arrow(ax, 5.8, 1.9, 6.9, 3.3, color=P["gold"], rad=-0.2, text="recovered\n(drain dir)", tx=6.2, ty=2.95, fs=6.5)
    arrow(ax, 8.7, 1.2, 10.0, 1.2, color=P["gold"], text="lost bytes", tx=9.35, ty=1.5, fs=6.5)
    # direct export path, drawn above the node layer
    ax.plot([1.35, 1.35, 11.4], [6.6, 7.75, 7.75], color=P["dark"], linewidth=0.9, linestyle=(0, (3, 2)))
    arrow(ax, 11.4, 7.75, 11.4, 5.9, ls=(0, (3, 2)))
    ax.text(6.05, 7.95, "direct export: OTel SDK to collector, skips 0.log", fontsize=7, ha="center")
    # gauge scrape, drawn below the node layer
    ax.plot([1.35, 1.35, 11.4], [4.1, 0.1, 0.1], color=P["teal"], linewidth=0.9, linestyle=":")
    arrow(ax, 11.4, 0.1, 11.4, 0.5, color=P["teal"], ls=":")
    ax.text(2.1, 0.25, "scraped", fontsize=6.8, color=P["teal"], ha="center")
    fig.tight_layout(pad=0.2)
    fig.savefig(os.path.join(FIG, "fig1_log_path.png"), dpi=300, facecolor="white")
    print("wrote fig1_log_path.png")


def fig2():
    fig, ax = plt.subplots(figsize=(6.5, 2.0))
    fig.patch.set_facecolor("white")
    ax.set_xlim(0, 19.0)
    ax.set_ylim(0, 4.3)
    ax.axis("off")
    seqs = list(range(10410, 10426))
    lost = {10412, 10413, 10414, 10423, 10424, 10425}
    for i, s in enumerate(seqs):
        x = 1.5 + i * 1.05
        ax.add_patch(FancyBboxPatch((x, 2.6), 0.9, 0.8, boxstyle="round,pad=0.02",
                                    facecolor=P["primary"], edgecolor=P["dark"], linewidth=0.6))
        ax.text(x + 0.45, 3.0, str(s)[-3:], color="white", fontsize=7, ha="center", va="center")
        if s in lost:
            ax.text(x + 0.45, 1.75, "x", color=P["rust"], fontsize=10, ha="center", va="center", weight="bold")
        else:
            ax.add_patch(FancyBboxPatch((x, 1.4), 0.9, 0.7, boxstyle="round,pad=0.02",
                                        facecolor=P["teal"], edgecolor=P["dark"], linewidth=0.6))
            ax.text(x + 0.45, 1.75, str(s)[-3:], color="white", fontsize=7, ha="center", va="center")
    ax.text(0.05, 3.0, "app", fontsize=7.5, va="center")
    ax.text(0.05, 1.75, "backend", fontsize=7.5, va="center")
    ax.text(9.6, 3.95, "sequence numbers 10410 to 10425 (last three digits shown)", fontsize=7.5, ha="center")
    x0, x1 = 1.5 + 2 * 1.05, 1.5 + 4 * 1.05 + 0.9
    ax.annotate("", (x0, 1.1), (x1, 1.1), arrowprops=dict(arrowstyle="<->", color=P["rust"], lw=0.9))
    ax.text((x0 + x1) / 2, 0.75, "internal gap, N = 3\n(visible: 10415 arrived)", fontsize=7, ha="center",
            va="center", color=P["rust"])
    x0, x1 = 1.5 + 13 * 1.05, 1.5 + 15 * 1.05 + 0.9
    ax.annotate("", (x0, 1.1), (x1, 1.1), arrowprops=dict(arrowstyle="<->", color=P["gold"], lw=0.9))
    ax.text((x0 + x1) / 2, 0.75, "tail gap, N = 3\nno later line reveals it", fontsize=7, ha="center",
            va="center", color=P["gold"])
    ax.text((x0 + x1) / 2, 0.15, "found only by the high-water\ngauge or the offset auditor", fontsize=6.8,
            ha="center", va="center", color=P["dark"])
    fig.tight_layout(pad=0.1)
    fig.savefig(os.path.join(FIG, "fig2_gap_types.png"), dpi=300, facecolor="white")
    print("wrote fig2_gap_types.png")


if __name__ == "__main__":
    fig1()
    fig2()

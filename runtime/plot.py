"""
Figures for the runtime check. Writes two figures per CSV:

- <csv>_gain.{pdf,png}: chopping gain (chopped / unchopped throughput) per trigger against
  clients, one panel per warehouse count, with a break-even line at 1.0. This is the claim:
  how much chopping gains under B-inc versus B-rec. Arms of one rep share a seed and run back to
  back, so the gain is computed per rep and averaged.
- <csv>.{pdf,png}: raw throughput and p95 latency, four arms per panel. Under 2PL contention
  shows as lock waiting, so p95 latency is the mechanism panel.

Encoding: color = trigger (B-inc blue, B-rec orange); in the raw figure, line style = chopping
(solid chopped, dashed unchopped). Bands are 95% t-confidence intervals over reps.

With --panels, every panel is also written as its own file into figures/ for LaTeX subfigures:
gain_wh<N>, throughput_wh<N>, p95_wh<N>, hold_wh<N> (.pdf and .png), the warehouse slice
*_by_wh_c<clients>, plus legend_gain and legend_raw as
separate legend strips. Panels carry no title or footnote (those go into the LaTeX captions),
have identical axes boxes so subfigures line up, and the gain panels share one y-range.

  python plot.py results_tmpfs_2pl.csv
  python plot.py results_tmpfs_2pl.csv --panels
  python plot.py results_tmpfs_2pl.csv --warehouses 1,2 --env "Linux, Postgres 16"
"""
import argparse
import csv
import math
import os
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COLOR = {"B-inc": "#2a78d6", "B-rec": "#eb6834"}      # validated pair (dataviz reference palette)
STYLE = {"chopped": "-", "unchopped": "--"}
ARMS = ["B-inc/chopped", "B-inc/unchopped", "B-rec/chopped", "B-rec/unchopped"]
METRICS = [("throughput_pps", "Throughput (Payments/s)"),
           ("latency_p95_ms", "p95 latency per Payment (ms)"),
           ("wlock_hold_p50_ms", "Warehouse-lock hold time, median (ms)")]
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e6e5e0"
DEFAULT_ENV = "Windows 11, Docker Desktop (WSL2), Postgres 16"
# two-sided 95% t quantiles for n-1 degrees of freedom (1.96 only beyond 30)
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
       10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110,
       18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
       26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def mean_ci(xs):
    n = len(xs)
    m = sum(xs) / n
    if n < 2:
        return m, 0.0
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m, T95.get(n - 1, 1.96) * sd / math.sqrt(n)


def load(path):
    """(warehouses, arm, clients, metric) -> {rep: value}, plus a label set for the footnote."""
    cells = defaultdict(dict)
    labels = set()
    for r in csv.DictReader(open(path, newline="")):
        if r["status"] != "OK":
            continue
        labels.add(f"{r['storage']}, {r['cc']}" + (f", {r['client']}" if r.get("client") else ""))
        for metric, _ in METRICS:
            cells[(int(r["warehouses"]), r["arm"], int(r["clients"]), metric)][r["rep"]] = \
                float(r[metric])
    return cells, labels


def style_axis(ax, xs):
    ax.set_xscale("log", base=2)
    ax.set_xticks(xs)
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.grid(True, axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def band(ax, xs, stats, color, style, label):
    ms = [s[0] for s in stats]
    ax.fill_between(xs, [m - c for m, c in stats], [m + c for m, c in stats],
                    color=color, alpha=0.15, linewidth=0)
    ax.plot(xs, ms, style, color=color, linewidth=1.5, marker="o", markersize=4,
            markeredgecolor="white", markeredgewidth=0.8, label=label)


def finish(fig, handles, labels, footnote, path_base, ncol):
    fig.legend(handles, labels, loc="upper center", ncol=ncol, frameon=False, fontsize=8,
               bbox_to_anchor=(0.5, 1.0), handlelength=3.5)
    fig.text(0.99, 0.005, footnote, ha="right", va="bottom", fontsize=7, color=MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    for ext in ("pdf", "png"):
        fig.savefig(f"{path_base}.{ext}", dpi=200)
    print(f"wrote {path_base}.pdf and {path_base}.png")


def raw_figure(cells, whs, xs_all, footnote, base):
    fig, axes = plt.subplots(len(whs), len(METRICS), figsize=(3.4 * len(METRICS), 2.3 * len(whs) + 0.6),
                             squeeze=False, sharex=True)
    for row, wh in enumerate(whs):
        for col, (metric, label) in enumerate(METRICS):
            ax = axes[row][col]
            for arm in ARMS:
                trig, mode = arm.split("/")
                xs = [x for x in xs_all if (wh, arm, x, metric) in cells]
                if xs:
                    band(ax, xs, [mean_ci(list(cells[(wh, arm, x, metric)].values()))
                                  for x in xs], COLOR[trig], STYLE[mode], f"{trig}, {mode}")
            style_axis(ax, xs_all)
            ax.set_ylim(bottom=0)
            if row == 0:
                ax.set_title(label, fontsize=9, color=INK, loc="left")
            if col == 0:
                ax.set_ylabel(f"{wh} warehouse{'s' if wh > 1 else ''}", color=INK)
            if row == len(whs) - 1:
                ax.set_xlabel("Clients")
    handles, labels = axes[0][0].get_legend_handles_labels()
    finish(fig, handles, labels, footnote, base, ncol=4)


def gain_figure(cells, whs, xs_all, footnote, base):
    ncols = 2 if len(whs) > 1 else 1
    nrows = math.ceil(len(whs) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(7.0, 2.5 * nrows + 0.6),
                             squeeze=False, sharex=True, sharey=True)
    metric = "throughput_pps"
    for i, wh in enumerate(whs):
        ax = axes[i // ncols][i % ncols]
        ax.axhline(1.0, color=MUTED, linewidth=0.8, zorder=1)
        for trig in ("B-inc", "B-rec"):
            xs, stats = [], []
            for x in xs_all:
                chop = cells.get((wh, f"{trig}/chopped", x, metric), {})
                unch = cells.get((wh, f"{trig}/unchopped", x, metric), {})
                reps = sorted(set(chop) & set(unch))
                if reps:
                    xs.append(x)
                    stats.append(mean_ci([chop[r] / unch[r] for r in reps]))
            if xs:
                band(ax, xs, stats, COLOR[trig], "-", f"{trig}: chopped / unchopped")
        style_axis(ax, xs_all)
        ax.set_title(f"{wh} warehouse{'s' if wh > 1 else ''}", fontsize=9, color=INK, loc="left")
        if i % ncols == 0:
            ax.set_ylabel("Throughput gain from chopping", color=INK)
        if i // ncols == nrows - 1:
            ax.set_xlabel("Clients")
    for j in range(len(whs), nrows * ncols):
        axes[j // ncols][j % ncols].set_visible(False)
    handles, labels = axes[0][0].get_legend_handles_labels()
    finish(fig, handles, labels, footnote + "; line at 1.0 = break-even", base, ncol=2)


def gain_stats(cells, wh, trig, xs_all):
    """(xs, [(mean, ci)]) of the per-rep gain chopped / unchopped."""
    xs, stats = [], []
    for x in xs_all:
        chop = cells.get((wh, f"{trig}/chopped", x, "throughput_pps"), {})
        unch = cells.get((wh, f"{trig}/unchopped", x, "throughput_pps"), {})
        reps = sorted(set(chop) & set(unch))
        if reps:
            xs.append(x)
            stats.append(mean_ci([chop[r] / unch[r] for r in reps]))
    return xs, stats


PANEL_YLABEL = {"throughput_pps": "Payments/s", "latency_p95_ms": "p95 latency (ms)",
                "wlock_hold_p50_ms": "Lock hold, median (ms)"}
PANEL_NAME = {"throughput_pps": "throughput", "latency_p95_ms": "p95",
              "wlock_hold_p50_ms": "hold"}
PANEL_SIZE = (3.2, 2.1)                          # inches; fits a 0.48\textwidth subfigure
PANEL_MARGINS = dict(left=0.19, right=0.97, bottom=0.21, top=0.96)


def save_panel(fig, path_base):
    fig.subplots_adjust(**PANEL_MARGINS)          # fixed margins: identical axes boxes
    for ext in ("pdf", "png"):
        fig.savefig(f"{path_base}.{ext}", dpi=200)
    plt.close(fig)


def save_legend(handles, labels, path_base, ncol):
    fig = plt.figure(figsize=(6.6, 0.3))
    fig.legend(handles, labels, loc="center", ncol=ncol, frameon=False, fontsize=8,
               handlelength=3.5)
    for ext in ("pdf", "png"):
        fig.savefig(f"{path_base}.{ext}", dpi=200, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def panels(cells, whs, xs_all, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    written = []
    # gain panels, one shared y-range
    gains = {(wh, t): gain_stats(cells, wh, t, xs_all) for wh in whs for t in ("B-inc", "B-rec")}
    lo = min(m - c for _, st in gains.values() for m, c in st)
    hi = max(m + c for _, st in gains.values() for m, c in st)
    pad = 0.05 * (hi - lo)
    gain_ylim = (min(0, lo - pad), hi + pad)
    for wh in whs:
        fig, ax = plt.subplots(figsize=PANEL_SIZE)
        ax.axhline(1.0, color=MUTED, linewidth=0.8, zorder=1)
        for trig in ("B-inc", "B-rec"):
            xs, st = gains[(wh, trig)]
            band(ax, xs, st, COLOR[trig], "-", f"{trig}: chopped / unchopped")
        style_axis(ax, xs_all)
        ax.set_ylim(*gain_ylim)
        ax.set_ylabel("Throughput gain", color=INK)
        ax.set_xlabel("Clients")
        if wh == whs[0]:
            gain_handles = ax.get_legend_handles_labels()
        save_panel(fig, os.path.join(out_dir, f"gain_wh{wh}"))
        written.append(f"gain_wh{wh}")
    save_legend(*gain_handles, os.path.join(out_dir, "legend_gain"), ncol=2)
    # raw panels
    names = PANEL_NAME
    for wh in whs:
        for metric, label in METRICS:
            fig, ax = plt.subplots(figsize=PANEL_SIZE)
            for arm in ARMS:
                trig, mode = arm.split("/")
                xs = [x for x in xs_all if (wh, arm, x, metric) in cells]
                if xs:
                    band(ax, xs, [mean_ci(list(cells[(wh, arm, x, metric)].values()))
                                  for x in xs], COLOR[trig], STYLE[mode], f"{trig}, {mode}")
            style_axis(ax, xs_all)
            ax.set_ylim(bottom=0)
            ax.set_ylabel(PANEL_YLABEL[metric], color=INK)
            ax.set_xlabel("Clients")
            if wh == whs[0] and metric == "throughput_pps":
                raw_handles = ax.get_legend_handles_labels()
            save_panel(fig, os.path.join(out_dir, f"{names[metric]}_wh{wh}"))
            written.append(f"{names[metric]}_wh{wh}")
    save_legend(*raw_handles, os.path.join(out_dir, "legend_raw"), ncol=4)
    print(f"wrote {len(written)} panels + 2 legends (.pdf, .png) to {out_dir}")
    return gain_ylim


def warehouse_slice(cells, whs, clients, out_dir, gain_ylim=None):
    """Second slice: warehouses on the x-axis at a fixed client count (the common TPC-C view:
    fewer warehouses = fewer hot w_ytd rows = more contention). Same encoding as the panels.
    gain_ylim: y-range shared with the grid gain panels, so both slices read on one scale."""
    if not any(k[2] == clients for k in cells):
        print(f"skipped the warehouse slice: no runs with {clients} clients "
              f"(choose another with --slice-clients)")
        return
    os.makedirs(out_dir, exist_ok=True)
    # gain
    fig, ax = plt.subplots(figsize=PANEL_SIZE)
    ax.axhline(1.0, color=MUTED, linewidth=0.8, zorder=1)
    for trig in ("B-inc", "B-rec"):
        xs, st = [], []
        for wh in whs:
            gx, gs = gain_stats(cells, wh, trig, [clients])
            if gx:
                xs.append(wh)
                st.append(gs[0])
        band(ax, xs, st, COLOR[trig], "-", f"{trig}: chopped / unchopped")
    style_axis(ax, whs)
    if gain_ylim:
        ax.set_ylim(*gain_ylim)
    else:
        ax.set_ylim(bottom=0)
    ax.set_ylabel("Throughput gain", color=INK)
    ax.set_xlabel(f"Warehouses ({clients} clients)")
    save_panel(fig, os.path.join(out_dir, f"gain_by_wh_c{clients}"))
    # raw
    names = PANEL_NAME
    for metric, _ in METRICS:
        fig, ax = plt.subplots(figsize=PANEL_SIZE)
        for arm in ARMS:
            trig, mode = arm.split("/")
            xs = [wh for wh in whs if (wh, arm, clients, metric) in cells]
            if xs:
                band(ax, xs, [mean_ci(list(cells[(wh, arm, clients, metric)].values()))
                              for wh in xs], COLOR[trig], STYLE[mode], f"{trig}, {mode}")
        style_axis(ax, whs)
        ax.set_ylim(bottom=0)
        ax.set_ylabel(PANEL_YLABEL[metric], color=INK)
        ax.set_xlabel(f"Warehouses ({clients} clients)")
        save_panel(fig, os.path.join(out_dir, f"{names[metric]}_by_wh_c{clients}"))
    print(f"wrote gain/throughput/p95/hold _by_wh_c{clients} (.pdf, .png) to {out_dir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--warehouses", default=None, help="comma-separated subset of rows")
    ap.add_argument("--env", default=DEFAULT_ENV, help="environment line for the footnote")
    ap.add_argument("--panels", action="store_true",
                    help="also write one file per panel to figures/ (for LaTeX subfigures)")
    ap.add_argument("--out-dir", default=None, help="panel directory (default: figures/)")
    ap.add_argument("--slice-clients", type=int, default=32,
                    help="client count for the warehouse slice written with --panels")
    args = ap.parse_args()

    cells, labels = load(args.csv)
    whs = sorted({k[0] for k in cells})
    if args.warehouses:
        whs = [w for w in whs if str(w) in args.warehouses.split(",")]
    xs_all = sorted({k[2] for k in cells})
    footnote = f"{args.env}; {'; '.join(sorted(labels))}; bands: 95% CI over reps"

    plt.rcParams.update({"font.size": 9, "axes.edgecolor": MUTED, "axes.labelcolor": INK,
                         "xtick.color": MUTED, "ytick.color": MUTED})
    base = os.path.splitext(args.csv)[0]
    raw_figure(cells, whs, xs_all, footnote, base)
    gain_figure(cells, whs, xs_all, footnote, base + "_gain")
    if args.panels:
        out_dir = args.out_dir or os.path.join(os.path.dirname(os.path.abspath(args.csv)),
                                               "figures")
        gain_ylim = panels(cells, whs, xs_all, out_dir)
        warehouse_slice(cells, whs, args.slice_clients, out_dir, gain_ylim)


if __name__ == "__main__":
    main()

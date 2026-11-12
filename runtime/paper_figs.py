"""Paper figures for the server campaign (E1-E3 and the reorder stage), in paperstyle.py's style.

One file per panel, no captions and no legends inside: captions are LaTeX subcaptions, and each
figure's legend is its own file, included above the panels. Every panel is drawn at the width it
is included at, with the same fixed axes box as the other panels of its figure, so subfigures
line up and fonts keep their printed size. The reference layout is paperstyle's: one column,
two panels across (0.48\\columnwidth each).
figures.tex assembles every figure (copy the blocks into the paper). Gains are computed per rep
(arms of a rep share a seed) and averaged; bands are 95% t-intervals.

    python paper_figs.py server_campaign                 # reads results_server_*.csv there
    python paper_figs.py server_campaign --out figures_paper --serif

Files (prefix = figure, suffix = panel; optional inputs are skipped when their CSV is missing):
    gain_wh1, gain_c32, legend_gain                 E1 / E2 gain chopped / unchopped
    throughput_wh1, throughput_c32, legend_arms     absolute Payments/s, four arms
    hold_wh1, hold_c32                              warehouse-lock hold (legend_arms)
    latency_wh1, latency_c32                        p95 latency (legend_arms)
    latency50_wh1, latency50_c32                    p50 latency (legend_arms)
    tthc_wh1, tthc_c32                              time to commit of the lock-holding piece
    mergegap_wh1, mergegap_c32, legend_mergegap     gain vs. merged and vs. @nomerge baseline
    e3_c8, e3_c32, legend_e3                        E3 (B-inc): every partition, best and worst
    e3hold, legend_e3hold                           E3: throughput vs. lock hold (one column)
    decomp_wh1_c8, ..._wh8_c32, legend_decomp       where a Payment's time goes (2 x 2)
    reorder_wh1, reorder_c32, legend_reorder        gain split into reordering x chopping
"""
import argparse
import csv
import logging
import math
import os
import statistics as st
from collections import defaultdict

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.ticker import FixedLocator, MaxNLocator, NullLocator

import paperstyle as ps

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

# Series beyond paperstyle's: each trigger's own unchopped baseline (the 2x2 design) and the
# gain factors of the reorder stage. Colour = trigger, line style = arm kind.
ps.SERIES.update({
    # one shape per trigger (Inc square, Rec triangle, as for the chopped arms); unchopped =
    # hollow marker on a dashed line
    "B-inc/unchopped": dict(color=ps.BLUE, marker="s", ls="--", mfc="white", label="B-inc unchopped"),
    "B-rec/unchopped": dict(color=ps.VERM, marker="v", ls="--", mfc="white", label="B-rec unchopped"),
    "B-inc/reorder": dict(color=ps.BLUE, marker="D", ls=":", label="B-inc: reordering only"),
    "B-rec/reorder": dict(color=ps.VERM, marker="D", ls=":", label="B-rec: reordering only"),
    "B-inc/chopping": dict(color=ps.BLUE, marker="s", ls="-", label="B-inc: chopping only"),
    "B-rec/chopping": dict(color=ps.VERM, marker="v", ls="-", label="B-rec: chopping only"),
})
# Printed names of the triggers: Inc and Rec (the arm names in the data keep chop.py's
# scenario names B-inc / B-rec).
NAME = {"B-inc": "Inc", "B-rec": "Rec"}
for _s in ps.SERIES.values():
    for _k, _v in NAME.items():
        _s["label"] = _s["label"].replace(_k, _v)
# Trigger colours: Inc dark navy, Rec dark red (formal, and they stay apart in greyscale print).
INC_COLOR, REC_COLOR = "#1F3A68", "#C00000"
for _s in ps.SERIES.values():
    _s["color"] = {ps.BLUE: INC_COLOR, ps.VERM: REC_COLOR}.get(_s["color"], _s["color"])
LEGEND_LINE = "#777777"       # line-style samples in legends: grey, so not read as Inc
TRIGGERS = ("B-inc", "B-rec")
COLOR = {"B-inc": INC_COLOR, "B-rec": REC_COLOR}
MARKER = {"B-inc": "s", "B-rec": "v"}
REORDER = {"B-inc": "B-inc/b6", "B-rec": "B-rec/b5"}
ARMS4 = ("B-inc/chopped", "B-rec/chopped", "B-inc/unchopped", "B-rec/unchopped")
T95 = {1: 12.71, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
       9: 2.262, 10: 2.228, 14: 2.145, 19: 2.093}

# Layouts: (panel width in inches, LaTeX subfigure width, environment, legend width).
# The panel width is exactly the subfigure width, so LaTeX includes it unscaled.
COLUMN2 = (0.48 * ps.COL_W, "0.48\\columnwidth", "figure", ps.COL_W)
MARGIN = dict(left=0.46, right=0.06, bottom=0.40, top=0.06)   # inches, same for every panel
PANEL_H = 1.25                # taller than paperstyle's 1.05: room for the 8 pt labels

# Paper typography on top of paperstyle (its colours, line widths and marker sizes stay):
# Times at 8 pt, closed black frame, inward ticks on all four sides, light horizontal
# gridlines only. plot_fig12_predictors.py uses the same settings.
PAPER_RC = {"font.family": "serif",
            "font.serif": ["Times New Roman", "Nimbus Roman", "DejaVu Serif"],
            "mathtext.fontset": "stix",
            "font.size": 8, "axes.labelsize": 8, "axes.titlesize": 8, "xtick.labelsize": 8,
            "ytick.labelsize": 8, "legend.fontsize": 8,
            "axes.spines.top": True, "axes.spines.right": True,
            "axes.edgecolor": "black", "xtick.color": "black", "ytick.color": "black",
            "xtick.direction": "in", "ytick.direction": "in",
            "xtick.top": True, "ytick.right": True,
            "axes.grid.axis": "y",
            "grid.color": "#E6E6E6", "grid.alpha": 1.0, "grid.linewidth": 0.3,
            # line weights for the printed size; dashes as wide as solid lines, with a
            # slightly longer pattern (in units of the line width) so they stay recognisable
            "lines.linewidth": 1.5, "lines.markersize": 4.5, "lines.markeredgewidth": 1.0,
            "lines.dashed_pattern": [4, 2],
            "axes.linewidth": 0.8, "xtick.major.width": 0.8, "ytick.major.width": 0.8,
            "xtick.minor.width": 0.6, "ytick.minor.width": 0.6}
SCATTER_S = 6                                                # marker area, ~ lines.markersize


def mean_ci(xs):
    """(mean, half-width of the 95% t-interval)."""
    m = st.mean(xs)
    if len(xs) < 2:
        return m, 0.0
    return m, T95.get(len(xs) - 1, 2.0) * st.stdev(xs) / math.sqrt(len(xs))


def load(path):
    """{(warehouses, clients): {rep: {arm: row}}} or None if the file is missing."""
    if not os.path.exists(path):
        return None
    out = defaultdict(lambda: defaultdict(dict))
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out[(int(r["warehouses"]), int(r["clients"]))][int(r["rep"])][r["arm"]] = r
    return out


def val(row, col):
    return float(row[col])


def stat(cell, arm, col):
    """Mean and CI of a column over the reps of a cell."""
    return mean_ci([val(reps[arm], col) for reps in cell.values() if arm in reps])


def gain(cell, num, den, col="throughput_pps"):
    """Per-rep ratio num/den, mean and CI."""
    return mean_ci([val(r[num], col) / val(r[den], col) for r in cell.values()
                    if num in r and den in r])


# ------------------------------------------------------------------------------------- drawing

def panel(width, height=PANEL_H):
    """One panel at its printed size, with the fixed axes box shared by every panel."""
    fig = plt.figure(figsize=(width, height))
    m = MARGIN
    ax = fig.add_axes([m["left"] / width, m["bottom"] / height,
                       1 - (m["left"] + m["right"]) / width, 1 - (m["bottom"] + m["top"]) / height])
    return fig, ax


CLIPPED = []


def save(fig, path):
    """ps.save, after checking that no label or tick text runs past the panel's edge (the
    fixed axes box leaves fixed margins; widen MARGIN if this reports anything). At 8 pt the
    default locator keeps only two or three y ticks on a short panel: ask for about five."""
    for ax in fig.axes:
        if ax.get_yscale() == "linear" and not isinstance(ax.yaxis.get_major_locator(),
                                                          FixedLocator):
            ax.yaxis.set_major_locator(MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]))
    fig.canvas.draw()
    box = fig.get_tightbbox(fig.canvas.get_renderer())
    w, h = fig.get_size_inches()
    if box.x0 < -0.005 or box.y0 < -0.005 or box.x1 > w + 0.005 or box.y1 > h + 0.005:
        CLIPPED.append(os.path.basename(path))
    ps.save(fig, path)


def legend_handles(handles, path, width, ncol=None, **kw):
    """A legend-only file at the width it is included at; taller when it needs more rows.
    kw goes to ax.legend (e.g. columnspacing for a tight row)."""
    ncol = ncol or len(handles)
    rows = math.ceil(len(handles) / ncol)
    fig = plt.figure(figsize=(width, 0.2 + 0.14 * (rows - 1)))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.legend(handles=handles, loc="center", ncol=ncol, frameon=False, borderpad=0.0, **kw)
    ps.save(fig, path)


def legend_series(keys, path, width, ncol=None):
    """legend_handles for paperstyle SERIES keys."""
    handles = []
    for k in keys:
        s = dict(ps.SERIES[k])
        label = s.pop("label")
        handles.append(mpl.lines.Line2D([], [], label=label, **s))
    legend_handles(handles, path, width, ncol)


def log2_axis(ax, xs):
    ax.set_xscale("log", base=2)
    ax.xaxis.set_major_locator(FixedLocator(xs))
    ax.xaxis.set_minor_locator(NullLocator())
    # at 1.6 in, seven labels collide: keep every other one (1, 4, 16, 64)
    keep = set(xs[::2]) | {xs[-1]} if len(xs) > 4 else set(xs)
    ax.set_xticklabels([str(x) if x in keep else "" for x in xs])


def thousands(ax):
    """"2k" instead of 2000: four digits would push the y label out of the panel."""
    if ax.get_ylim()[1] >= 1000 and ax.get_yscale() == "linear":
        ax.yaxis.set_major_formatter(kfmt)


def kfmt(v, _=None):
    return f"{v / 1000:g}k" if v >= 1000 else f"{v:g}"


def series(ax, xs, cells, key, f):
    """Plot one series; f(cell) -> (mean, ci)."""
    ms = [f(c) for c in cells]
    ps.line(ax, xs, [m for m, _ in ms], key,
            lo=[m - h for m, h in ms], hi=[m + h for m, h in ms])


def slices(e1, e2, clients=32):
    """The two reported slices: clients at 1 warehouse (E1), and warehouses at 32 clients (E2,
    plus the 1-warehouse point from E1)."""
    s1 = [(c, e1[(1, c)]) for (w, c) in sorted(e1) if w == 1]
    s2 = [(1, e1[(1, clients)])] if (1, clients) in e1 else []
    s2 += [(w, e2[(w, c)]) for (w, c) in sorted(e2) if c == clients]
    return s1, s2


LATENCY_YLIM = (1, 1000)    # p50 and p95 latency panels share this range, so they compare


def pair(name, s1, s2, draw, ylabel, out, logy=False, sharey=True, zero=True, ylim=None):
    """Two panels, <name>_wh1 (clients at 1 warehouse) and <name>_c32 (warehouses at 32
    clients), with the same axes box and, if sharey, the same y-range."""
    figs = []
    for suffix, sl, xname in (("wh1", s1, "Clients"), ("c32", s2, "Warehouses")):
        if not sl:
            continue
        fig, ax = panel(COLUMN2[0])
        xs = [x for x, _ in sl]
        draw(ax, xs, [c for _, c in sl])
        log2_axis(ax, xs)
        ax.set_xlabel(xname)
        ax.set_ylabel(ylabel)
        if logy:
            ax.set_yscale("log")
        elif zero:
            ax.set_ylim(bottom=0)
        figs.append((fig, ax, f"{out}/{name}_{suffix}"))
    if sharey and len(figs) > 1:
        lo = min(ax.get_ylim()[0] for _, ax, _ in figs)
        hi = max(ax.get_ylim()[1] for _, ax, _ in figs)
        for _, ax, _ in figs:
            ax.set_ylim(lo, hi)
    if ylim:                                   # fixed range, to compare across figures
        for _, ax, _ in figs:
            ax.set_ylim(*ylim)
    for fig, ax, path in figs:
        thousands(ax)
        save(fig, path)
    return [p for _, _, p in figs]


# ------------------------------------------------------------------------------------- figures

def fig_gain(s1, s2, out):
    def draw(ax, xs, cells):
        ps.breakeven(ax)
        for t in TRIGGERS:
            series(ax, xs, cells, f"{t}/chopped", lambda c: gain(c, f"{t}/chopped", f"{t}/unchopped"))
    legend_series(["B-inc/chopped", "B-rec/chopped"], f"{out}/legend_gain", COLUMN2[3])
    return pair("gain", s1, s2, draw, "Throughput gain", out)


def arms_pair(name, col, ylabel, s1, s2, out, **kw):
    def draw(ax, xs, cells):
        for arm in ARMS4:
            series(ax, xs, cells, arm, lambda c: stat(c, arm, col))
    return pair(name, s1, s2, draw, ylabel, out, **kw)


def fig_mergegap(s1, s2, out):
    def draw(ax, xs, cells):
        ps.breakeven(ax)
        for t in TRIGGERS:
            series(ax, xs, cells, f"{t}/chopped", lambda c: gain(c, f"{t}/chopped", f"{t}/unchopped"))
            series(ax, xs, cells, f"{t}/nomerge",
                   lambda c: gain(c, f"{t}/chopped", f"{t}/unchopped@nomerge"))
    legend_series(["B-inc/chopped", "B-rec/chopped", "B-inc/nomerge", "B-rec/nomerge"],
                  f"{out}/legend_mergegap", COLUMN2[3], ncol=2)
    return pair("mergegap", s1, s2, draw, "Throughput gain", out)


def e3_points(cell):
    """[(trigger, arm, pieces, gain mean, gain ci, hold p50, throughput)] for one E3 cell."""
    out = []
    for arm in sorted({a for reps in cell.values() for a in reps}):
        t = arm.split("/")[0]
        g, h = gain(cell, arm, f"{t}/unchopped")
        row = next(reps[arm] for reps in cell.values() if arm in reps)
        out.append((t, arm, int(row["pieces"]), g, h, stat(cell, arm, "wlock_hold_p50_ms")[0],
                    stat(cell, arm, "throughput_pps")[0]))
    return out


def scatter_handle(t, label, marker=None):
    return mpl.lines.Line2D([], [], ls="none", marker=marker or MARKER[t], mfc="white",
                            mec=COLOR[t], label=label)


E3_CLIENTS = (8, 32)          # 64 clients has 32's shape; B-rec has no wrong merge (caption)
DOT_S = 6
# whether pa3 ends its piece: colour and shape differ, so the split survives greyscale; the
# filled marker is translucent and the other hollow, so overlaps stay visible
GROUP = {True: dict(color=ps.BLUE, face=mpl.colors.to_rgba(ps.BLUE, 0.5), marker="o",
                    label="pa3 ends its piece"),
         False: dict(color=ps.ORANGE, face="none", marker="^",
                     label="pa3 does not end its piece")}


def ends_pa3(arm):
    """Whether pa3 (the second finest piece of B-inc) is the last op of its piece: some block
    of the partition ends after the second finest piece."""
    kind = arm.split("/")[1]
    if kind in ("unchopped", "chopped"):
        return kind == "chopped"
    ends, acc = set(), 0
    for size in kind[1:].split("."):
        acc += int(size)
        ends.add(acc)
    return 2 in ends


def fig_e3(e3, out, trigger="B-inc"):
    """Throughput of every contiguous partition against its number of pieces, one marker each
    at its exact piece count, with its 95% CI, marked by whether pa3 ends its piece. The point
    at one piece is the unchopped arm."""
    figs = []
    for c in E3_CLIENTS:
        if (1, c) not in e3:
            continue
        cell = e3[(1, c)]
        by_j = defaultdict(list)
        for p in e3_points(cell):
            if p[0] == trigger:
                by_j[p[2]].append((*stat(cell, p[1], "throughput_pps"), p[1]))
        js = sorted(by_j)
        fig, ax = panel(COLUMN2[0])
        for j in js:
            for m, h, arm in by_j[j]:
                st_ = GROUP[ends_pa3(arm)]
                ax.errorbar(j, m, yerr=h, fmt="none", ecolor=st_["color"], elinewidth=0.4,
                            alpha=0.6, capsize=0, zorder=2)
                ax.scatter(j, m, s=DOT_S, marker=st_["marker"], zorder=3,
                           facecolors=st_["face"], edgecolors=st_["color"], linewidths=0.5)
        ax.set_xticks(js)
        ax.set_xlim(min(js) - 0.5, max(js) + 0.5)
        ax.set_xlabel("Pieces")
        ax.set_ylabel("Payments/s")
        figs.append((fig, ax, f"{out}/e3_c{c}"))
    hi = max(ax.get_ylim()[1] for _, ax, _ in figs)
    for fig, ax, path in figs:
        ax.set_ylim(0, hi)
        thousands(ax)
        save(fig, path)
    legend_handles([
        mpl.lines.Line2D([], [], ls="none", marker=s["marker"], mfc=s["face"], mec=s["color"],
                         label=s["label"]) for s in (GROUP[True], GROUP[False])
    ], f"{out}/legend_e3", COLUMN2[3], ncol=2)
    return [p for _, _, p in figs]


def fig_e3_hold(e3, out):
    """Throughput against the warehouse-lock hold: at 1 warehouse every Payment holds the same
    row, so throughput <= 1 / hold, whatever the partition."""
    fig, ax = panel(ps.COL_W, 2 * PANEL_H)
    shapes = {8: "o", 32: "s", 64: "^"}
    holds = []
    for (w, c), cell in sorted(e3.items()):
        for t in TRIGGERS:
            pts = [p for p in e3_points(cell) if p[0] == t]
            holds += [p[5] for p in pts]
            ax.scatter([p[5] for p in pts], [p[6] for p in pts], s=SCATTER_S,
                       marker=shapes.get(c, "o"), facecolors="none", edgecolors=COLOR[t],
                       linewidths=0.6)
    lo, hi = min(holds) * 0.8, max(holds) * 1.2
    xs = [lo * (hi / lo) ** (i / 50) for i in range(51)]
    ax.plot(xs, [1000 / x for x in xs], color=LEGEND_LINE, ls="--")
    ax.set_xscale("log")
    ax.set_yscale("log")
    for axis, ticks in ((ax.xaxis, [0.2, 0.5, 1, 2]), (ax.yaxis, [500, 1000, 2000, 4000])):
        axis.set_major_locator(FixedLocator(ticks))
        axis.set_minor_locator(NullLocator())
        axis.set_major_formatter(kfmt)
    ax.set_xlabel("Warehouse-lock hold, median (ms)")
    ax.set_ylabel("Payments/s")
    save(fig, f"{out}/e3hold")
    handles = [scatter_handle(t, NAME[t]) for t in TRIGGERS]
    handles += [mpl.lines.Line2D([], [], ls="none", marker=m, mfc="white", mec=LEGEND_LINE,
                                 label=f"{c} clients") for c, m in shapes.items()]
    handles.append(mpl.lines.Line2D([], [], color=LEGEND_LINE, ls="--", label="1 / hold"))
    legend_handles(handles, f"{out}/legend_e3hold", ps.COL_W, ncol=3)
    return [f"{out}/e3hold"]


DECOMP = [  # (label, colour, hatch, f(row) -> ms)
    ("lock wait", ps.ORANGE, "////", lambda r: val(r, "sampler_lockwait_ms")),
    ("execution", ps.SKY, "", lambda r: max(0.0, val(r, "t_stmt_ms") - val(r, "sampler_lockwait_ms"))),
    ("commit", ps.GREEN, "", lambda r: val(r, "t_commit_ms")),
    ("client gaps", ps.PINK, "", lambda r: val(r, "t_gap_intra_ms") + val(r, "t_gap_inter_ms")),
    ("retries", "#999999", "", lambda r: val(r, "t_retry_ms")),
]
ARM_SHORT = {"B-inc/unchopped": "Inc\nunch.", "B-inc/chopped": "Inc\nchop.",
             "B-rec/unchopped": "Rec\nunch.", "B-rec/chopped": "Rec\nchop."}


def fig_decomp(e1, e2, out):
    cells = [((1, c), e1.get((1, c))) for c in (8, 32, 64)] + [((8, 32), e2.get((8, 32)))]
    cells = [(k, v) for k, v in cells if v]
    paths = []
    with mpl.rc_context({"hatch.linewidth": 0.5}):
        for (w, c), cell in cells:
            fig, ax = panel(COLUMN2[0])
            arms = list(ARM_SHORT)
            bottom = [0.0] * len(arms)
            for label, color, hatch, f in DECOMP:
                vals = [st.mean(f(reps[a]) for reps in cell.values() if a in reps) for a in arms]
                ax.bar(range(len(arms)), vals, bottom=bottom, color=color, hatch=hatch,
                       edgecolor="white", linewidth=0.4, width=0.7)
                bottom = [b + v for b, v in zip(bottom, vals)]
            ax.set_xticks(range(len(arms)))
            ax.set_xticklabels([ARM_SHORT[a] for a in arms])
            ax.tick_params(axis="x", length=0, pad=1)
            ax.grid(axis="x", visible=False)
            ax.set_ylabel("ms per Payment")
            paths.append(f"{out}/decomp_wh{w}_c{c}")
            save(fig, paths[-1])
        legend_handles([mpl.patches.Patch(facecolor=col, hatch=h, edgecolor="white", label=lab)
                        for lab, col, h, _ in DECOMP], f"{out}/legend_decomp", COLUMN2[3],
                       handlelength=1.2, handletextpad=0.4, columnspacing=0.8)  # one row
    return paths


def fig_reorder(ro, out):
    """Total gain = reordering (one piece, chop.py order / TPC-C order) x chopping (chopped /
    one piece in chop.py order). Per rep, so the product holds rep by rep."""
    s1 = [(c, ro[(1, c)]) for (w, c) in sorted(ro) if w == 1]
    s2 = [(w, ro[(w, c)]) for (w, c) in sorted(ro) if c == 32]

    def draw(ax, xs, cells):
        ps.breakeven(ax)
        for t in TRIGGERS:
            series(ax, xs, cells, f"{t}/reorder", lambda c: gain(c, REORDER[t], f"{t}/unchopped"))
            series(ax, xs, cells, f"{t}/chopping", lambda c: gain(c, f"{t}/chopped", REORDER[t]))
    legend_series(["B-inc/chopping", "B-rec/chopping", "B-inc/reorder", "B-rec/reorder"],
                  f"{out}/legend_reorder", COLUMN2[3], ncol=2)
    return pair("reorder", s1, s2, draw, "Throughput gain", out)


# ----------------------------------------------------------------------------------- LaTeX

def figure_block(legend, files, captions, caption, label, layout=COLUMN2, per_row=2):
    """One figure: legend strip, then the panels as subfigures, per_row to a line."""
    _, subw, env, _ = layout
    lw = "\\textwidth" if env == "figure*" else "\\columnwidth"
    lines = [f"\\begin{{{env}}}[t]", "  \\centering",
             f"  \\includegraphics[width={lw}]{{\\figdir/{legend}}}\\\\[2pt]"]
    if len(files) == 1:
        lines.append(f"  \\includegraphics[width={lw}]{{\\figdir/{os.path.basename(files[0])}}}")
    for i, (f, cap) in enumerate(zip(files, captions) if len(files) > 1 else []):
        name = os.path.basename(f)
        if i:
            lines.append("  \\\\[4pt]" if i % per_row == 0 else "  \\hfill")
        lines += [f"  \\begin{{subfigure}}[t]{{{subw}}}",
                  f"    \\includegraphics[width=\\linewidth]{{\\figdir/{name}}}",
                  f"    \\caption{{{cap}}}\\label{{fig:{name.replace('_', '-')}}}",
                  "  \\end{subfigure}"]
    lines += [f"  \\caption{{{caption}}}", f"  \\label{{fig:{label}}}", f"\\end{{{env}}}", ""]
    return "\n".join(lines)


SLICE_CAPS = ["1 warehouse", "32 clients"]


def write_tex(out, made):
    """figures.tex: one block per figure. Captions are drafts; \\figdir is the folder of the
    figure files in the paper (set at the top with \\providecommand)."""
    blocks = ["% Generated by paper_figs.py. Needs \\usepackage{subcaption}.",
              "% Every panel is drawn at its included width: keep the subfigure widths as they are.",
              "\\providecommand{\\figdir}{figures}", ""]
    B = lambda *a, **k: blocks.append(figure_block(*a, **k))
    if "gain" in made:
        B("legend_gain", made["gain"], SLICE_CAPS,
          "Throughput gain of chopping (chopped / unchopped, per trigger), TPC-C Payment.", "gain")
    if "throughput" in made:
        B("legend_arms", made["throughput"], SLICE_CAPS, "Throughput of the four arms.",
          "throughput")
    if "hold" in made:
        B("legend_arms", made["hold"], SLICE_CAPS,
          "Time the warehouse lock is held per Payment (median).", "hold")
    if "latency" in made:
        B("legend_arms", made["latency"], SLICE_CAPS, "p95 latency of a Payment.", "latency")
    if "latency50" in made:
        B("legend_arms", made["latency50"], SLICE_CAPS, "Median (p50) latency of a Payment.",
          "latency50")
    if "tthc" in made:
        B("legend_arms", made["tthc"], SLICE_CAPS,
          "Time to the commit of the piece holding the warehouse lock (median).", "tthc")
    if "mergegap" in made:
        B("legend_mergegap", made["mergegap"], SLICE_CAPS,
          "Gain against the unchopped arm with (solid) and without (dashed) the merged customer "
          "update.", "mergegap")
    if "decomp" in made:
        B("legend_decomp", made["decomp"],
          [os.path.basename(f).replace("decomp_wh", "").replace("_c", " wh, ") + " clients"
           for f in made["decomp"]],
          "Where a Payment's time goes (mean per Payment).", "decomp")
    if "e3" in made:
        B("legend_e3", made["e3"],
          [f"{os.path.basename(f).split('_c')[1]} clients" for f in made["e3"]],
          "Throughput of every contiguous partition of Inc's finest chopping, by number of "
          "pieces (1 warehouse, 95\\% CI). One piece is the unchopped arm. The 16 partitions "
          "in which pa3 ends its piece all lie above the 16 in which it does not. At 64 clients the "
          "shape equals 32 clients. Under Rec all partitions with 2--5 pieces lie within "
          "5\\% of each other.", "e3")
    if "e3hold" in made:
        B("legend_e3hold", made["e3hold"], [""],
          "Throughput against warehouse-lock hold, every E3 arm (1 warehouse).", "e3hold")
    if "reorder" in made:
        B("legend_reorder", made["reorder"], SLICE_CAPS,
          "Gain split into reordering (one piece, chop.py's order vs. TPC-C's order) and "
          "chopping proper (chopped vs. one piece in chop.py's order).", "reorder")
    with open(f"{out}/figures.tex", "w", encoding="utf-8") as f:
        f.write("\n".join(blocks))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", help="directory with results_server_*.csv")
    ap.add_argument("--out", default=None, help="output directory (default: <data>/figures_paper)")
    ap.add_argument("--serif", action="store_true", help="Times-like fonts, to match the body text")
    args = ap.parse_args()
    out = args.out or os.path.join(args.data, "figures_paper")
    os.makedirs(out, exist_ok=True)
    ps.use(serif=args.serif)
    mpl.rcParams.update(PAPER_RC)
    mpl.rcParams["savefig.bbox"] = None        # keep the exact panel size and axes box
    path = lambda name: os.path.join(args.data, f"results_server_{name}.csv")
    e1, e2, e3, ro = load(path("e1")), load(path("e2")), load(path("e3")), load(path("reorder"))
    made = {}
    if e1 and e2:
        s1, s2 = slices(e1, e2)
        made["gain"] = fig_gain(s1, s2, out)
        # colour = trigger, line style = arm: four entries in one row
        Line = mpl.lines.Line2D
        long = {"B-inc": "incremental", "B-rec": "recomputing"}
        legend_handles([Line([], [], ls="none", marker=MARKER[t], color=COLOR[t], label=long[t])
                        for t in TRIGGERS]
                       + [Line([], [], color=LEGEND_LINE, ls="-", label="chopped"),
                          Line([], [], color=LEGEND_LINE, ls="--", label="unchopped")],
                       f"{out}/legend_arms", COLUMN2[3], columnspacing=0.8, handletextpad=0.4)
        made["throughput"] = arms_pair("throughput", "throughput_pps", "Payments/s", s1, s2, out,
                                       sharey=False)
        made["hold"] = arms_pair("hold", "wlock_hold_p50_ms", "Lock hold (ms)", s1, s2, out)
        made["latency"] = arms_pair("latency", "latency_p95_ms", "p95 latency (ms)", s1, s2, out,
                                    logy=True, ylim=LATENCY_YLIM)
        made["latency50"] = arms_pair("latency50", "latency_p50_ms", "p50 latency (ms)", s1, s2,
                                      out, logy=True, ylim=LATENCY_YLIM)
        made["tthc"] = arms_pair("tthc", "tthc_p50_ms", "Commit time (ms)", s1, s2, out,
                                 logy=True)
        made["mergegap"] = fig_mergegap(s1, s2, out)
        made["decomp"] = fig_decomp(e1, e2, out)
    if e3:
        made["e3"] = fig_e3(e3, out)
        made["e3hold"] = fig_e3_hold(e3, out)
    if ro:
        made["reorder"] = fig_reorder(ro, out)
    write_tex(out, made)
    n = sum(len(v) for v in made.values())
    print(f"wrote {n} panels, their legends and figures.tex to {out}")
    if CLIPPED:
        print("CLIPPED (text past the panel edge; widen MARGIN):", ", ".join(CLIPPED))


if __name__ == "__main__":
    main()

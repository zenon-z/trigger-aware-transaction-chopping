"""Fig. 12, predictor version: E3's B-inc partitions against two x-axes side by side.

Left column: number of pieces. Right column: ops_after_pa3, the number of operations after pa3
inside pa3's piece, i.e. executed while the warehouse lock is held (0 = pa3 ends its piece).
{pa5 pa7} counts as two, so the values are 0, 1, 2, 4, 5. Rows: 8 and 32 clients at 1
warehouse. Same data, means and CIs as paper_figs.fig_e3 (no new runs); that figure and its
script stay as they are. One colour and marker for every point, no legend;
fig12_predictors.tex holds the LaTeX block.

    python plot_fig12_predictors.py server_campaign   # -> figures_paper/fig12_predictors*

In the left panels, points that would overlap are offset sideways by at most --spread (a
swarm, in a fixed order); the right panels put every point at its exact x. Prints the checks: 32 points per
panel, counts per x value, min/max throughput per ops_after_pa3 value.
"""
import argparse
import math
import os

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.ticker import FixedLocator

import paperstyle as ps
import paper_figs as pf
from payment import finest_pieces

TRIGGER = "B-inc"
CLIENTS = (8, 32)
FINEST = finest_pieces(TRIGGER)          # ((pa2,), (pa3,), (pa4,), (pc,), (pa5, pa7), (pa6,))
K = len(FINEST)
PA3 = next(i for i, p in enumerate(FINEST) if "pa3" in p) + 1     # 1-based: 2
DOT_FACE = mpl.colors.to_rgba(ps.BLUE, 0.5)
DOT_D = math.sqrt(pf.DOT_S) / 72         # marker diameter in inches (s is the area in pt^2)
SWARM_STEPS = 2                          # slots on each side: 0, +-spread/2, +-spread
YMAX = 3000


def blocks(arm):
    """Block sizes of a partition over the finest pieces: B-inc/b2.4 -> [2, 4]."""
    kind = arm.split("/")[1]
    if kind == "unchopped":
        return [K]
    if kind == "chopped":
        return [1] * K
    return [int(s) for s in kind[1:].split(".")]


def ops_after_pa3(arm):
    """Operations after pa3 inside pa3's piece: the rest of pa3's finest piece plus every
    operation of the finest pieces merged after it."""
    end = 0
    for size in blocks(arm):
        end += size
        if end >= PA3:
            own = FINEST[PA3 - 1]
            return (len(own) - 1 - own.index("pa3")
                    + sum(len(p) for p in FINEST[PA3:end]))


def partitions(cell):
    """[(arm, pieces, ops_after_pa3, mean, ci)] for every B-inc partition of one cell."""
    out = []
    for p in pf.e3_points(cell):
        if p[0] == TRIGGER:
            m, h = pf.stat(cell, p[1], "throughput_pps")
            out.append((p[1], p[2], ops_after_pa3(p[1]), m, h))
    return out


def swarm(pts, x, spread, sx, sy):
    """Offsets for the points of one x value: each point, in a fixed order (by partition),
    takes the first of 0, -d, +d, -2d, +2d, ... (|offset| <= spread) where it overlaps no
    point placed before it; if every slot overlaps, the slot farthest from its neighbours.
    sx, sy: inches per data unit, so overlap is judged at the printed size."""
    steps = SWARM_STEPS
    cands = [0.0] + [s * spread * k / steps for k in range(1, steps + 1) for s in (-1, 1)]
    placed, out = [], {}
    for arm, m in sorted(pts):
        def gap(dx):
            return min((math.hypot((dx - px) * sx, (m - py) * sy) for px, py in placed),
                       default=math.inf)
        dx = next((c for c in cands if gap(c) >= DOT_D), None)
        if dx is None:
            dx = max(cands, key=gap)
        placed.append((dx, m))
        out[arm] = x + dx
    return out


def strip(ax, pts, xof, spread):
    """Every partition at its x; with spread > 0, points that would overlap are offset
    sideways (swarm, at most +-spread), the others stay at x. One colour and marker for all;
    the translucent fill shows remaining overlaps as darker spots."""
    by_x = {}
    for p in pts:
        by_x.setdefault(xof(p), []).append(p)
    xlim = (min(by_x) - 0.5, max(by_x) + 0.5)
    ax.set_xlim(*xlim)
    ax.set_ylim(0, YMAX)
    pos, (W, H) = ax.get_position(), ax.figure.get_size_inches()
    sx, sy = pos.width * W / (xlim[1] - xlim[0]), pos.height * H / YMAX
    for x, group in by_x.items():
        xs = (swarm([(p[0], p[3]) for p in group], x, spread, sx, sy) if spread
              else {p[0]: x for p in group})
        for arm, _, ops, m, h in group:
            ax.errorbar(xs[arm], m, yerr=h, fmt="none", ecolor=ps.BLUE, elinewidth=0.4,
                        alpha=0.6, capsize=0, zorder=2)
            ax.scatter(xs[arm], m, s=pf.DOT_S, marker="o", zorder=3, facecolors=DOT_FACE,
                       edgecolors=ps.BLUE, linewidths=0.5)
    ax.set_xticks(range(min(by_x), max(by_x) + 1))      # every integer: no point at 3 shows
    return {x: len(g) for x, g in by_x.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", help="directory with results_server_e3.csv")
    ap.add_argument("--out", default=None, help="output directory (default: <data>/figures_paper)")
    ap.add_argument("--spread", type=float, default=0.12,
                    help="left panels: largest sideways offset of a point that would overlap "
                         "another (0 = none); the right panels are never spread")
    ap.add_argument("--serif", action="store_true")
    ap.add_argument("--band", action="store_true",
                    help="write only the best/worst band version of the four panels "
                         "(fig12_band_*, legend_fig12_band, fig12_band.tex)")
    ap.add_argument("--minmax", action="store_true",
                    help="write only the max/min version in paper typography "
                         "(fig12_minmax_*, legend_fig12_minmax, fig12_minmax.tex)")
    args = ap.parse_args()
    out = args.out or os.path.join(args.data, "figures_paper")
    os.makedirs(out, exist_ok=True)
    ps.use(serif=args.serif)
    mpl.rcParams["savefig.bbox"] = None
    e3 = pf.load(os.path.join(args.data, "results_server_e3.csv"))
    if args.band or args.minmax:
        look = "minmax" if args.minmax else "band"
        prefix = LOOKS[look]["prefix"]
        paths = band_panels(e3, out, args.spread, look)
        with open(os.path.join(out, f"{prefix}.tex"), "w", encoding="utf-8") as f:
            f.write(TEX_SPLIT.replace("fig12_", f"{prefix}_").replace("e3-", f"e3-{look}-")
                    .replace("  \\centering\n", "  \\centering\n  \\includegraphics"
                             f"[width=\\columnwidth]{{\\figdir/legend_{prefix}}}\\\\[2pt]\n"))
        if pf.CLIPPED:
            print("text runs past the figure edge:", pf.CLIPPED)
        print("wrote", ", ".join(os.path.basename(p) for p in paths),
              f"legend_{prefix} and {prefix}.tex")
        return

    # fixed margins (inches) at the printed size: one column, two panels across
    W, H = ps.COL_W, 2 * ps.PANEL_H + 0.57
    left, right, top, bottom, wgap, hgap = 0.52, 0.06, 0.04, 0.43, 0.12, 0.34
    fig, axes = plt.subplots(2, 2, figsize=(W, H), sharey=True)
    fig.subplots_adjust(left=left / W, right=1 - right / W, top=1 - top / H, bottom=bottom / H,
                        wspace=wgap / ((W - left - right - wgap) / 2),
                        hspace=hgap / ((H - top - bottom - hgap) / 2))
    for r, c in enumerate(CLIENTS):
        pts = partitions(e3[(1, c)])
        assert len(pts) == 32, (c, len(pts))
        cut = {p[0] for p in pts if pf.ends_pa3(p[0])}
        assert cut == {p[0] for p in pts if p[2] == 0}, "ops_after_pa3 == 0 != fig_e3's blue set"
        n_left = strip(axes[r][0], pts, lambda p: p[1], args.spread)
        n_right = strip(axes[r][1], pts, lambda p: p[2], 0)
        assert [n_left[j] for j in sorted(n_left)] == [1, 5, 10, 10, 5, 1], n_left
        assert sum(n_right.values()) == 32, n_right
        print(f"{c} clients: {len(cut)} partitions with ops_after_pa3 = 0 (= fig_e3's blue)")
        print(f"  pieces:        counts {dict(sorted(n_left.items()))}")
        print(f"  ops_after_pa3: counts {dict(sorted(n_right.items()))}")
        for x in sorted(n_right):
            ms = [p[3] for p in pts if p[2] == x]
            print(f"    {x}: min {min(ms):6.0f}  max {max(ms):6.0f} Payments/s")
        axes[r][0].set_ylabel(f"{c} clients\nPayments/s")
    for ax in axes.flat:
        ax.yaxis.set_major_locator(FixedLocator([0, 1000, 2000, 3000]))
        ax.yaxis.set_major_formatter(pf.kfmt)
        ax.grid(axis="x", visible=False)               # horizontal gridlines only
    for ax in axes[:, 1]:
        ax.tick_params(axis="y", labelleft=False)     # shared y: labels on the left only
    for ax in axes[0]:
        ax.tick_params(axis="x", labelbottom=True)
    axes[1][0].set_xlabel("Pieces")
    axes[1][1].set_xlabel("Operations executed while\nholding the warehouse lock")
    path = os.path.join(out, "fig12_predictors")
    pf.save(fig, path)
    with open(path + ".tex", "w", encoding="utf-8") as f:
        f.write(TEX)
    split = split_panels(e3, out, args.spread)
    with open(os.path.join(out, "fig12_split.tex"), "w", encoding="utf-8") as f:
        f.write(TEX_SPLIT)
    if pf.CLIPPED:
        print("text runs past the figure edge:", pf.CLIPPED)
    print("wrote", path + ".pdf and .tex;", ", ".join(os.path.basename(p) for p in split),
          "and fig12_split.tex")


# The four panels as separate files, for subfigures at 0.48\columnwidth each. Same figure size
# and axes box (inches) in all four; the bottom margin fits the two-line x label of the
# operations panels in every panel, so the boxes line up side by side and across rows.
SPLIT_W = 0.48 * ps.COL_W
SPLIT_H = 1.35
SPLIT_MARGIN = dict(left=0.40, right=0.06, bottom=0.43, top=0.05)
XLABEL = {"pieces": "Pieces", "ops": "Operations executed while\nholding the warehouse lock"}


def split_panels(e3, out, spread):
    paths = []
    for c in CLIENTS:
        pts = partitions(e3[(1, c)])
        for kind in ("pieces", "ops"):
            fig = plt.figure(figsize=(SPLIT_W, SPLIT_H))
            m = SPLIT_MARGIN
            ax = fig.add_axes([m["left"] / SPLIT_W, m["bottom"] / SPLIT_H,
                               1 - (m["left"] + m["right"]) / SPLIT_W,
                               1 - (m["bottom"] + m["top"]) / SPLIT_H])
            if kind == "pieces":
                strip(ax, pts, lambda p: p[1], spread)
            else:
                strip(ax, pts, lambda p: p[2], 0)
            ax.yaxis.set_major_locator(FixedLocator([0, 1000, 2000, 3000]))
            ax.yaxis.set_major_formatter(pf.kfmt)
            ax.grid(axis="x", visible=False)
            ax.set_ylabel("Payments/s")
            ax.set_xlabel(XLABEL[kind])
            paths.append(os.path.join(out, f"fig12_{kind}_{c}c"))
            pf.save(fig, paths[-1])
    return paths


BAND_GREY, BAND_WORST = "#BBBBBB", "#444444"

# Two looks of the band panels. "band": the earlier blue version (fig12_band_*). "minmax":
# paper typography (Times, 8 pt), closed black frame, inward ticks, black max/min lines, grey
# dots and a light grey band (fig12_minmax_*). Typography, line widths and marker sizes are
# paper_figs.PAPER_RC, as in the other figures.
LOOKS = {
    "band": dict(prefix="fig12_band", w=SPLIT_W, h=SPLIT_H, margin=SPLIT_MARGIN, rc={},
                 best=ps.BLUE, worst=BAND_WORST, dot=BAND_GREY, dot_s=pf.DOT_S,
                 band=dict(color=ps.BLUE, alpha=0.12),
                 labels=("best partition", "worst partition", "other partitions",
                         "range across partitions"), legend_ncol=2, legend_h=None),
    "minmax": dict(prefix="fig12_minmax", w=SPLIT_W, h=1.55,
                   margin=dict(left=0.46, right=0.06, bottom=0.56, top=0.06),
                   rc=pf.PAPER_RC,         # the paper's typography and line weights
                   best="black", worst="black", dot="#9A9A9A", dot_s=4,
                   band=dict(color="#E3E3E3", alpha=1.0),
                   labels=("max", "min", "other partitions", "min–max range"),
                   legend_ncol=4, legend_h=0.2),
}


def band_panels(e3, out, spread, look="band"):
    """The four split panels in the max/min style: grey dots for the other partitions
    (swarm within +-spread on the pieces panels, exact x on the operations panels), the best
    partition per x as a solid line with filled markers, the worst as a dashed line with
    hollow markers, both with their CIs, and the range between them shaded. x values without
    any partition (3 operations) break the lines and the band."""
    L = LOOKS[look]
    with mpl.rc_context(L["rc"]):
        return _band_panels(e3, out, spread, L)


def _band_panels(e3, out, spread, L):
    W, H, m = L["w"], L["h"], L["margin"]
    paths = []
    for c in CLIENTS:
        pts = partitions(e3[(1, c)])
        for kind, xof, spr in (("pieces", lambda p: p[1], spread), ("ops", lambda p: p[2], 0)):
            fig = plt.figure(figsize=(W, H))
            ax = fig.add_axes([m["left"] / W, m["bottom"] / H,
                               1 - (m["left"] + m["right"]) / W, 1 - (m["bottom"] + m["top"]) / H])
            by_x = {}
            for p in pts:
                by_x.setdefault(xof(p), []).append(p)
            xs = list(range(min(by_x), max(by_x) + 1))
            xlim = (xs[0] - 0.5, xs[-1] + 0.5)
            ax.set_xlim(*xlim)
            ax.set_ylim(0, YMAX)
            pos = ax.get_position()
            sx, sy = pos.width * W / (xlim[1] - xlim[0]), pos.height * H / YMAX
            pick = lambda f: [f(by_x[x], key=lambda p: p[3]) if x in by_x else None for x in xs]
            best, worst = pick(max), pick(min)
            nan = float("nan")
            ys = lambda sel: [p[3] if p else nan for p in sel]
            ax.fill_between(xs, ys(worst), ys(best), linewidth=0, zorder=1, **L["band"])
            for x in by_x:                     # the other partitions
                rest = [p for p in by_x[x] if p is not best[xs.index(x)]
                        and p is not worst[xs.index(x)]]
                if not rest:
                    continue
                pos_x = (swarm([(p[0], p[3]) for p in rest], x, spr, sx, sy) if spr
                         else {p[0]: x for p in rest})
                ax.scatter([pos_x[p[0]] for p in rest], [p[3] for p in rest], s=L["dot_s"],
                           color=L["dot"], linewidths=0, zorder=2)
            # worst first, so where best = worst (one partition) the filled marker shows
            for sel, color, ls, mfc in ((worst, L["worst"], "--", "white"),
                                        (best, L["best"], "-", L["best"])):
                ax.plot(xs, ys(sel), color=color, ls=ls, zorder=3)
                have = [(x, p) for x, p in zip(xs, sel) if p]
                ax.errorbar([x for x, _ in have], [p[3] for _, p in have],
                            yerr=[p[4] for _, p in have], fmt="o", color=color, mfc=mfc,
                            mec=color, elinewidth=0.5, capsize=0, zorder=4)
            ax.set_xticks(xs)
            ax.yaxis.set_major_locator(FixedLocator([0, 1000, 2000, 3000]))
            ax.yaxis.set_major_formatter(pf.kfmt)
            ax.grid(axis="x", visible=False)
            ax.set_ylabel("Payments/s")
            ax.set_xlabel(XLABEL[kind])
            paths.append(os.path.join(out, f"{L['prefix']}_{kind}_{c}c"))
            pf.save(fig, paths[-1])
    lab = L["labels"]
    handles = [
        mpl.lines.Line2D([], [], color=L["best"], marker="o", mfc=L["best"], label=lab[0]),
        mpl.lines.Line2D([], [], color=L["worst"], ls="--", marker="o", mfc="white",
                         label=lab[1]),
        mpl.lines.Line2D([], [], ls="none", marker="o", mfc=L["dot"], mec="none", label=lab[2]),
        mpl.patches.Patch(label=lab[3], **{"facecolor" if k == "color" else k: v
                                           for k, v in L["band"].items()}),
    ]
    legend = os.path.join(out, f"legend_{L['prefix']}")
    if L["legend_h"] is None:
        pf.legend_handles(handles, legend, ps.COL_W, ncol=L["legend_ncol"])
    else:
        fig = plt.figure(figsize=(ps.COL_W, L["legend_h"]))
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.legend(handles=handles, loc="center", ncol=L["legend_ncol"], frameon=False,
                  borderpad=0.0)
        ps.save(fig, legend)
    return paths


TEX_SPLIT = r"""% Generated by plot_fig12_predictors.py. Every panel is drawn at 0.48\columnwidth with
% the same axes box: keep the subfigure widths as they are.
\providecommand{\figdir}{figures}
\begin{figure}[t]
  \centering
  \begin{subfigure}[t]{0.48\columnwidth}
    \includegraphics[width=\linewidth]{\figdir/fig12_pieces_8c}
    \caption{8 clients}\label{fig:e3-pieces-8c}
  \end{subfigure}
  \hfill
  \begin{subfigure}[t]{0.48\columnwidth}
    \includegraphics[width=\linewidth]{\figdir/fig12_ops_8c}
    \caption{8 clients}\label{fig:e3-ops-8c}
  \end{subfigure}
  \\[4pt]
  \begin{subfigure}[t]{0.48\columnwidth}
    \includegraphics[width=\linewidth]{\figdir/fig12_pieces_32c}
    \caption{32 clients}\label{fig:e3-pieces-32c}
  \end{subfigure}
  \hfill
  \begin{subfigure}[t]{0.48\columnwidth}
    \includegraphics[width=\linewidth]{\figdir/fig12_ops_32c}
    \caption{32 clients}\label{fig:e3-ops-32c}
  \end{subfigure}
  \caption{Throughput of every contiguous partition of Inc's finest chopping (1 warehouse,
    95\% CI). Left: by number of pieces; partitions with the same number of pieces that would
    overlap are offset sideways. Right: by the number of operations after pa3 (the district
    update that fires the CPR) in its piece, i.e.\ executed while the warehouse lock is held;
    0 means pa3 ends its piece and the lock is released right after the update. One piece is
    the unchopped arm.}
  \label{fig:e3-split}
\end{figure}
"""


TEX = r"""% Generated by plot_fig12_predictors.py. Drawn at \columnwidth: include it unscaled.
\providecommand{\figdir}{figures}
\begin{figure}[t]
  \centering
  \includegraphics[width=\columnwidth]{\figdir/fig12_predictors}
  \caption{Throughput of every contiguous partition of Inc's finest chopping (1 warehouse,
    95\% CI). Left: by number of pieces; partitions with the same number of pieces are spread
    sideways. Right: by the number of operations after pa3 (the district update that fires the
    CPR) in its piece, i.e.\ executed while the warehouse lock is held; 0 means pa3 ends its
    piece and the lock is released right after the update. One piece is the unchopped arm.}
  \label{fig:e3-predictors}
\end{figure}
"""


if __name__ == "__main__":
    main()

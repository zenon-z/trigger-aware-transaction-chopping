"""Matplotlib style for IEEEtran two-column papers.

Draw figures at their FINAL printed size. Never render large and scale down in
LaTeX -- that is what makes fonts look oversized next to body text.

    import paperstyle as ps
    ps.use()
    fig, axes = ps.panels(2, 2)          # one \\figure*  (full width)
    fig, ax   = ps.panels(1, 1, full=False)   # one \\figure (one column)
    ...
    ps.legend(fig, axes[0][0], ncol=4)
    ps.save(fig, "figures/gain_wh1")     # writes .pdf

In LaTeX:  \\includegraphics[width=\\columnwidth]{figures/gain_wh1}
           \\includegraphics[width=\\textwidth]{figures/gain_grid}
"""

import matplotlib as mpl
import matplotlib.pyplot as plt

# ---------------------------------------------------------------- dimensions
COL_W = 3.45      # IEEEtran \columnwidth in inches
FULL_W = 7.16     # IEEEtran \textwidth in inches
PANEL_H = 1.05    # one panel's height; 2x2 in a column ~ 2.6in tall

# ------------------------------------------------------------------- palette
# Okabe-Ito: distinguishable for the common colour-vision deficiencies, and
# the lightness differences survive greyscale printing.
BLACK = "#000000"
BLUE = "#0072B2"
GREEN = "#009E73"
VERM = "#D55E00"
PINK = "#CC79A7"
ORANGE = "#E69F00"
SKY = "#56B4E9"

# One place to define every series. Colour AND marker AND linestyle, so no
# single channel carries the distinction on its own.
SERIES = {
    "unchopped":        dict(color=BLACK, marker="o", ls="--", label="unchopped"),
    "unchopped@nomerge": dict(color=BLACK, marker="o", ls=":",  label="unchopped, no merge"),
    "B-inc/chopped":    dict(color=BLUE,  marker="s", ls="-",  label="B-inc chopped"),
    "B-rec/chopped":    dict(color=VERM,  marker="v", ls="-",  label="B-rec chopped"),
    "B-inc/nomerge":    dict(color=BLUE,  marker="s", ls="--", label="B-inc vs. no-merge"),
    "B-rec/nomerge":    dict(color=VERM,  marker="v", ls="--", label="B-rec vs. no-merge"),
}


def use(serif=False):
    """Install the rcParams. serif=True matches IEEEtran's Times body text."""
    mpl.rcParams.update({
        # --- fonts: sized for 10pt body text ---------------------------------
        "font.family": "serif" if serif else "sans-serif",
        "font.serif": ["Times New Roman", "Nimbus Roman", "DejaVu Serif"],
        "font.sans-serif": ["Helvetica", "Nimbus Sans", "DejaVu Sans"],
        # Four panels inside one 3.45in column leaves each about 1.6in wide,
        # so these are deliberately small. Do not scale the figure in LaTeX.
        "font.size": 6,
        "axes.labelsize": 6.5,
        "axes.titlesize": 6.5,
        "xtick.labelsize": 5.5,
        "ytick.labelsize": 5.5,
        "legend.fontsize": 6,
        "mathtext.fontset": "stix" if serif else "dejavusans",

        # --- lines and markers ----------------------------------------------
        "lines.linewidth": 0.8,
        "lines.markersize": 2.3,
        "lines.markeredgewidth": 0.6,

        # --- axes -------------------------------------------------------------
        "axes.linewidth": 0.6,
        "axes.grid": True,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "grid.linewidth": 0.4,
        "grid.alpha": 0.5,
        "grid.color": "#B0B0B0",

        # --- ticks ------------------------------------------------------------
        "xtick.major.width": 0.6,
        "ytick.major.width": 0.6,
        "xtick.major.size": 2.5,
        "ytick.major.size": 2.5,
        "xtick.direction": "out",
        "ytick.direction": "out",

        # --- legend -----------------------------------------------------------
        "legend.frameon": False,
        "legend.handlelength": 2.0,
        "legend.columnspacing": 1.2,
        "legend.handletextpad": 0.5,
        "legend.borderaxespad": 0.0,

        # --- output -----------------------------------------------------------
        "figure.dpi": 150,
        "savefig.dpi": 300,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.01,
        "pdf.fonttype": 42,     # embed TrueType, not Type3 -- some venues reject Type3
        "ps.fonttype": 42,
    })


def panels(nrows=1, ncols=1, full=False, panel_h=PANEL_H, sharex=False, sharey=False):
    """A panel grid at final printed size. Default is ONE COLUMN wide, so a 2x2
    grid gives four small panels, two across the column -- the reference layout.
    Pass full=True only for a \\figure* that genuinely spans both columns.

    Returns (fig, axes) with axes always 2-D, so axes[0][0] works for 1x1."""
    width = FULL_W if full else COL_W
    fig, axes = plt.subplots(
        nrows, ncols,
        figsize=(width, panel_h * nrows + 0.26 * nrows),
        sharex=sharex, sharey=sharey,
        squeeze=False,
        constrained_layout=True,
    )
    return fig, axes


def thin_xticks(ax, values, keep):
    """Label only `keep` of `values` on the x-axis. At 1.6in wide a panel fits
    three or four tick labels, not seven."""
    ax.set_xticks(list(values))
    ax.set_xticklabels([str(v) if v in keep else "" for v in values])


def line(ax, x, y, key, lo=None, hi=None, **kw):
    """Plot one series by SERIES key, with an optional min-max / CI band."""
    s = dict(SERIES[key])
    label = s.pop("label")
    s.update(kw)
    ax.plot(x, y, label=label, **s)
    if lo is not None and hi is not None:
        ax.fill_between(x, lo, hi, color=s["color"], alpha=0.15, linewidth=0)


def xlabel(ax, axis_name, caption=None):
    """x-axis label plus, on a second line, the panel caption -- e.g.

        xlabel(ax, "Clients", "(a) 1 warehouse, varying clients")

    Both go into the xlabel so constrained_layout reserves the right space;
    free-floating text does not get measured and drifts."""
    ax.set_xlabel(axis_name if caption is None else f"{axis_name}\n{caption}")


def legend(fig, ax, ncol=4, y=1.02):
    """One shared legend above the grid, taken from `ax`'s handles."""
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, y),
               ncol=ncol, frameon=False)


def legend_file(keys, path_without_ext, ncol=None, full=True):
    """Render ONLY the legend, as its own file, for \\includegraphics above the
    panels.

    Draw it at the same width you will include it at, otherwise its text ends up
    a different size from the axis labels -- LaTeX scales the whole file, fonts
    included, so a legend drawn at 3.45in and included at \\textwidth comes out
    about twice as large as the tick labels next to it.

        legend_file(["B-inc/chopped", "B-rec/chopped", "unchopped"],
                    "figures/legend_gain")
    """
    width = FULL_W if full else COL_W
    ncol = ncol or len(keys)
    fig = plt.figure(figsize=(width, 0.22))
    ax = fig.add_subplot(111)
    ax.set_axis_off()
    handles = []
    for k in keys:
        s = dict(SERIES[k])
        label = s.pop("label")
        handles.append(mpl.lines.Line2D([], [], label=label, **s))
    ax.legend(handles=handles, loc="center", ncol=ncol, frameon=False,
              mode=None, borderpad=0.0)
    fig.savefig(f"{path_without_ext}.pdf")
    fig.savefig(f"{path_without_ext}.png", dpi=300)
    plt.close(fig)


def breakeven(ax, value=1.0):
    """The horizontal reference line for gain plots."""
    ax.axhline(value, color="#444444", linewidth=0.7, zorder=1)


def save(fig, path_without_ext):
    """PDF for LaTeX (vector), PNG alongside for quick viewing."""
    fig.savefig(f"{path_without_ext}.pdf")
    fig.savefig(f"{path_without_ext}.png", dpi=300)
    plt.close(fig)

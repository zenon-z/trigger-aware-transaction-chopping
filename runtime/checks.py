"""
Verdicts of the server's checks before and during the E1-E3 campaign. Every
comparison is within one batch; per-rep ratios where the arms of a rep share a seed.

  python3 checks.py overhead results_server_overhead.csv
      timing on vs. off (@notiming): pass if |ratio - 1| < 2% or the CI contains 1
  python3 checks.py merge results_server_merge.csv
      unchopped with pa5+pa6 as two statements (@nomerge) vs. one (MODEL.md R15): what the
      merge is worth (a measurement, not a gate: the arms stay realistic)
  python3 checks.py locklog results_server_locklog_off.csv results_server_locklog_on.csv
      cost of log_lock_waits with deadlock_timeout = 1 ms (separate containers, so a ratio of
      means): adopt only if < 2%; plus the lock-wait estimates side by side
  python3 checks.py drift results_server_drift_*.csv
      throughput per arm in every drift batch against the first one: flag beyond 5%

Exit code 1 if any check fails, so a script can stop on it.
"""
import csv
import math
import sys
from collections import defaultdict

from plot import mean_ci


def rows_of(path):
    return [r for r in csv.DictReader(open(path, newline="", encoding="utf-8"))]


def bad_rows(rows):
    return [r for r in rows if r["status"] != "OK" or float(r["deadlocks"] or 0)]


def per_rep(rows, metric="throughput_pps"):
    """{(warehouses, clients, arm): {rep: value}}"""
    out = defaultdict(dict)
    for r in rows:
        out[(int(r["warehouses"]), int(r["clients"]), r["arm"])][r["rep"]] = float(r[metric])
    return out


def ratio(num, den):
    reps = sorted(set(num) & set(den))
    return mean_ci([num[k] / den[k] for k in reps]) if reps else None


def pct(stat):
    m, c = stat
    return f"{(m - 1) * 100:+6.1f}% ± {c * 100:4.1f}%"


def overhead(paths):
    rows = [r for p in paths for r in rows_of(p)]
    cells, ok = per_rep(rows), True
    print("timing on vs. off: throughput(@notiming) / throughput(timed) - 1, per rep")
    for (wh, cl, arm), vals in sorted(cells.items()):
        if arm.endswith("@notiming"):
            continue
        s = ratio(cells.get((wh, cl, arm + "@notiming"), {}), vals)
        if not s:
            continue
        passed = abs(s[0] - 1) < 0.02 or abs(s[0] - 1) <= s[1]
        ok &= passed
        print(f"  wh={wh} c={cl:3d} {arm:18s} {pct(s)}  {'pass' if passed else 'FAIL'}")
    print("verdict:", "timing stays on for every Payment" if ok else
          "timing costs throughput: rerun the campaign with TIMING_EVERY=10")
    return ok


def merge(paths):
    rows = [r for p in paths for r in rows_of(p)]
    cells, worst = per_rep(rows), 0.0
    print("unchopped, two customer statements vs. one: throughput(@nomerge) / merged - 1")
    for (wh, cl, arm), vals in sorted(cells.items()):
        if arm.endswith("@nomerge"):
            continue
        s = ratio(cells.get((wh, cl, arm + "@nomerge"), {}), vals)
        if s:
            worst = max(worst, abs(s[0] - 1))
            print(f"  wh={wh} c={cl:3d} {arm:18s} {pct(s)}")
    # decided Oct 28: realistic arms (unchopped merges, chopped forfeits the merge where it
    # splits pa5/pa6); E1/E2 carry the @nomerge baselines, so this is a measurement, not a gate
    print(f"result: the merge is worth up to {worst:.1%} of the unchopped arm's throughput; "
          "E1/E2 report how much of the chopped/unchopped gap it explains (plot_stages "
          "mergegap)")
    return True


def welch(a, b):
    """Ratio of means b/a with an approximate 95% CI (delta method)."""
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1) / len(a) if len(a) > 1 else 0.0
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1) / len(b) if len(b) > 1 else 0.0
    r = mb / ma
    return r, 1.96 * r * math.sqrt(va / ma ** 2 + vb / mb ** 2)


def locklog(paths):
    off, on = rows_of(paths[0]), rows_of(paths[1])
    ok = True
    print("logging lock waits (deadlock_timeout = 1 ms) vs. not: throughput on / off - 1")
    for arm in sorted({r["arm"] for r in off}):
        a = [float(r["throughput_pps"]) for r in off if r["arm"] == arm]
        b = [float(r["throughput_pps"]) for r in on if r["arm"] == arm]
        if not (a and b):
            continue
        s = welch(a, b)
        passed = abs(s[0] - 1) < 0.02
        ok &= passed
        print(f"  {arm:18s} {pct(s)}  {'pass' if passed else 'FAIL'}")
    print("lock wait per Payment, ms (mean over reps)")
    cols = [("locklog_wait_ms_per_payment", "logged>=1ms"), ("t_lockwait_lo_ms", "est. lo"),
            ("t_lockwait_hi_ms", "est. hi"), ("sampler_lockwait_ms", "sampler"),
            ("wlock_wait_mean_ms", "warehouse"), ("pg_cpu_util", "pg cpu")]
    print(f"  {'':10s} {'arm':18s} " + " ".join(f"{h:>11s}" for _, h in cols))
    for tag, rows in (("off", off), ("on", on)):
        for arm in sorted({r["arm"] for r in rows}):
            rs = [r for r in rows if r["arm"] == arm]
            vals = []
            for c, _ in cols:
                xs = [float(r[c]) for r in rs if r[c] not in ("", "nan")]
                xs = [x for x in xs if not math.isnan(x)]
                vals.append(f"{sum(xs) / len(xs):11.2f}" if xs else f"{'-':>11s}")
            print(f"  {'log ' + tag:10s} {arm:18s} " + " ".join(vals))
    print("verdict:", "logging is cheap enough for the breakdown cells" if ok else
          "logging changes throughput: use the estimates (bracket, sampler) only")
    return True


def drift(paths):
    batches = []
    for p in sorted(paths, key=lambda p: ("start", "mid", "end").index(p.rsplit("_", 1)[1][:-4])
                    if p.rsplit("_", 1)[1][:-4] in ("start", "mid", "end") else 9):
        rows = [r for r in rows_of(p) if r["status"] == "OK"]
        means = defaultdict(list)
        for r in rows:
            means[r["arm"]].append(float(r["throughput_pps"]))
        batches.append((p, {a: sum(v) / len(v) for a, v in means.items()}))
    if not batches:
        return True
    ok = True
    ref_path, ref = batches[0]
    print(f"drift against {ref_path} (mean throughput per arm)")
    for p, means in batches:
        devs = {a: means[a] / ref[a] - 1 for a in means if a in ref}
        worst = max((abs(d) for d in devs.values()), default=0.0)
        ok &= worst <= 0.05
        print(f"  {p}: " + "  ".join(f"{a}={d:+.1%}" for a, d in sorted(devs.items()))
              + ("" if worst <= 0.05 else "   <-- DRIFT > 5%"))
    return ok


def main(argv):
    if len(argv) < 2 or argv[0] not in ("overhead", "merge", "locklog", "drift"):
        print(__doc__)
        return 2
    paths = argv[1:]
    bad = [r for p in paths for r in bad_rows(rows_of(p))]
    if bad:
        print(f"{len(bad)} rows FLAG or with deadlocks: check them first")
    ok = {"overhead": overhead, "merge": merge, "locklog": locklog, "drift": drift}[argv[0]](paths)
    return 0 if ok and not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

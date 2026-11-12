"""
Quick check of sweep CSVs (both workloads): problems first, then mean throughput, warehouse-lock
hold time and where the clients wait, per (layout, warehouses, clients, arm).

  python3 summarize.py results_server_*smoke*.csv
"""
import csv
import statistics as st
import sys
from collections import defaultdict

VIOLATION = "_violations"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def main(paths):
    bad = 0
    for path in paths:
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        if not rows:
            print(f"\n{path}: empty")
            continue
        mix = "throughput_tps" in rows[0]
        tput = "throughput_tps" if mix else "throughput_pps"
        flags = [r for r in rows if r["status"] != "OK"]
        deadlocks = sum(num(r["deadlocks"]) for r in rows)
        violations = sum(num(v) for r in rows for k, v in r.items() if k.endswith(VIOLATION))
        bad += len(flags) + (violations > 0)
        print(f"\n{path}: {len(rows)} rows, {len(flags)} FLAG, {deadlocks:.0f} deadlocks, "
              f"{violations:.0f} violations" + ("  <-- CHECK" if flags or violations else ""))
        cells = defaultdict(list)
        for r in rows:
            cells[(r.get("district_layout", ""), int(r["warehouses"]), int(r["clients"]),
                   r["arm"])].append(r)
        print(f"  {'layout':7s} {'wh':>3s} {'cl':>4s} {'arm':16s} {'tput':>8s} {'hold':>6s} "
              f"{'wait':>7s}  top waits (fraction of clients)")
        for (layout, wh, cl, arm), rs in sorted(cells.items()):
            mean = lambda k: st.mean(num(r.get(k)) for r in rs)
            waits = sorted(((mean(k), k) for k in rs[0] if k.startswith(("wait_", "in_"))),
                           reverse=True)[:3]
            print(f"  {layout:7s} {wh:3d} {cl:4d} {arm:16s} {mean(tput):8.1f} "
                  f"{mean('wlock_hold_p50_ms'):6.2f} {mean('wlock_wait_p50_ms'):7.2f}  "
                  + "  ".join(f"{k}={v:.2f}" for v, k in waits if v >= 0.01))
        if "t_e2e_ms" in rows[0]:            # Payment only, E1-E3 columns (MODEL.md R22)
            print(f"  {'wh':>3s} {'cl':>4s} {'arm':24s} {'e2e':>7s} {'sum':>7s} {'ttfc':>6s} "
                  f"{'tthc':>6s} {'lw lo':>6s} {'lw hi':>6s} {'sampl':>6s} {'wh-wt':>6s} "
                  f"{'cpu':>5s}  (ms per Payment, means)")
            tcells = defaultdict(list)
            for r in rows:
                tcells[(int(r["warehouses"]), int(r["clients"]), r["arm"])].append(r)
            for (wh, cl, arm), rs in sorted(tcells.items()):
                mean = lambda k: st.mean(num(r.get(k)) for r in rs)
                parts = sum(mean(f"t_{c}_ms") for c in
                            ("stmt", "commit", "gap_intra", "gap_inter", "retry"))
                print(f"  {wh:3d} {cl:4d} {arm:24s} {mean('t_e2e_ms'):7.2f} {parts:7.2f} "
                      f"{mean('ttfc_mean_ms'):6.2f} {mean('tthc_mean_ms'):6.2f} "
                      f"{mean('t_lockwait_lo_ms'):6.2f} {mean('t_lockwait_hi_ms'):6.2f} "
                      f"{mean('sampler_lockwait_ms'):6.2f} {mean('wlock_wait_mean_ms'):6.2f} "
                      f"{mean('pg_cpu_util'):5.2f}")
    print("\ntput = Payments/s (Payment only) or transactions/s (mix); hold, wait = median "
          "warehouse-lock hold and wait per Payment, ms")
    print("e2e = timed end to end, sum = its components (must match); lw lo/hi = lock-wait "
          "bracket; sampl = sampler's mean lock wait; wh-wt = measured warehouse-lock wait")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

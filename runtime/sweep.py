"""
Sweep driver for the runtime check: 4 arms x warehouses x clients x reps.

Each (warehouses) block reloads the data; each run resets the trigger, truncates History and
vacuums. Arm order is shuffled per (rep, clients) with a fixed seed, so slow drift in the
container does not line up with one arm. One CSV row per run, appended, so an interrupted sweep
can be resumed with --resume (finished cells are skipped).

  python sweep.py --storage tmpfs                       # main sweep (container pg-tmpfs)
  python sweep.py --storage disk --warehouses 1 --clients 32   # real-disk check point
  python sweep.py --storage tmpfs --quick               # smoke test, about 3 min (see --help)
  python sweep.py --storage tmpfs --workload mix        # full TPC-C mix (mix.py), same grid
  python sweep.py --storage tmpfs --contiguous --warehouses 1 --clients 32 --experiment E3

Payment only: every run also appends its summed statement times per calibration key to a
sidecar (<out>_stmts.jsonl), from which calibrate.py builds the lock-wait baselines and which
allows the lock-wait estimate to be recomputed offline against another baseline.

The full mix reloads the whole TPC-C database at the start of every rep (a rep = all client
counts and arms of one warehouse count), so the database cannot grow without bound over many
reps; within a rep it is never reset, and each run checks the TPC-C consistency conditions on
the whole database instead (mix.py). With --mix-check light, the full check runs before every
reload and at the end of each warehouse block.
"""
import argparse
import csv
import json
import os
import random
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mix  # noqa: E402
import payment  # noqa: E402

FIELDS = ["code_version", "experiment", "storage", "fsync", "synchronous_commit", "server_version",
          "pg_cpus", "host", "client_procs", "client_cpus", "cc", "rep", "arm", "warehouses",
          "clients", "seed", "pieces", "district_layout"] + payment.WAIT_FIELDS + [
          "throughput_pps", "payments", "retries_per_payment", "wytd_piece",
          "wytd_retries_per_payment", "retries_by_piece", "deadlocks", "serialization_failures",
          "latency_mean_ms", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
          "wlock_hold_p50_ms", "wlock_hold_p95_ms", "wlock_wait_p50_ms", "wlock_wait_p95_ms",
          "wlock_wait_mean_ms"] + payment.TIMING_FIELDS + payment.MXACT_FIELDS + [
          "abandoned", "cc1_violations", "cc8_violations", "cc9_violations",
          "customer_violations", "status"]


def preflight(dsn):
    """Abort unless no other client is connected: a second client would add load to the run."""
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_stat_activity "
                    "WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()")
        others = cur.fetchone()[0]
    conn.close()
    if others:
        sys.exit(f"ABORT: {others} other client(s) connected; the measurement would be shared.")


def ints(s):
    return [int(x) for x in s.split(",")]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--storage", choices=["tmpfs", "disk"], required=True,
                    help="which container is running; selects the DSN and labels every row")
    ap.add_argument("--workload", choices=["payment", "mix"], default="payment",
                    help="payment = Payment only (default); mix = full TPC-C mix (2pl only)")
    ap.add_argument("--district", choices=payment.LAYOUTS, default="split",
                    help="split = d_ytd in its own table (default, MODEL.md R14); joined = stock "
                         "TPC-C layout, for the check that quantifies the split")
    ap.add_argument("--mix-check", choices=["full", "light"], default="full",
                    help="mix: consistency check per run; light = conditions 1, 2, 3, 8, 9 per "
                         "run and the full check at the end of each warehouse block")
    ap.add_argument("--cc", choices=sorted(payment.CC), default="2pl",
                    help="2pl = strict 2PL on rows (default); ssi = SERIALIZABLE")
    ap.add_argument("--warehouses", type=ints, default=[1, 2, 4, 8])
    ap.add_argument("--clients", type=ints, default=[1, 2, 4, 8, 16, 32, 64])
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--duration-s", type=float, default=30.0)
    ap.add_argument("--warmup-s", type=float, default=2.0)
    ap.add_argument("--prewarm-s", type=float, default=0.0,
                    help="Payment only: one discarded run of this length after each load, before "
                         "the first rep (the first run on a fresh instance was up to 13%% low "
                         "on the server); default 0 = none")
    ap.add_argument("--out", default=None, help="CSV path (default: results_<storage>_<cc>.csv)")
    ap.add_argument("--resume", action="store_true", help="skip cells already in the CSV")
    ap.add_argument("--arms", default=",".join(payment.ARMS),
                    help="comma-separated subset of arms (default: all four)")
    ap.add_argument("--coarsen", action="store_true",
                    help="run the coarsening arms of both triggers instead (B-inc/k6 ... B-rec/k1)")
    ap.add_argument("--contiguous", action="store_true",
                    help="run E3's arms instead: per trigger unchopped, chopped and every "
                         "contiguous partition of the finest chopping (48 arms)")
    ap.add_argument("--experiment", default="", help="label recorded per row (E1, E3, calib, ...)")
    ap.add_argument("--timing-every", type=int, default=1,
                    help="Payment only: time breakdown for every Nth Payment per client "
                         "(0 = none; default 1)")
    ap.add_argument("--calibration", default="",
                    help="Payment only: statement baselines from calibrate.py (lock-wait columns)")
    ap.add_argument("--stmts-out", default=None,
                    help="Payment only: sidecar JSONL with statement times per run "
                         "(default: <out>_stmts.jsonl)")
    ap.add_argument("--procs", type=int, default=1, help="client processes per run (default 1)")
    ap.add_argument("--client-cpus", default="", help="pin clients to cores, e.g. 32-47 (Linux)")
    ap.add_argument("--pg-cpus", default="",
                    help="label: cores Postgres is pinned to (the compose cpuset), recorded per row")
    ap.add_argument("--dsn", default=None, help="override the DSN chosen by --storage")
    ap.add_argument("--quick", action="store_true",
                    help="smoke test: 1 and 8 wh, clients 1,4, 1 rep, 5 s (writes *_quick.csv)")
    args = ap.parse_args()

    is_mix = args.workload == "mix"
    if is_mix and (args.cc != "2pl" or args.coarsen or args.contiguous):
        sys.exit("the full mix runs under 2pl with the four main arms only")
    suffix = "_mix" if is_mix else ""
    if args.quick:
        args.warehouses, args.clients, args.reps = [1, 8], [1, 4], 1
        args.duration_s, args.warmup_s = 5.0, 1.0
        args.out = args.out or os.path.join(payment.HERE,
                                            f"results_{args.storage}_{args.cc}{suffix}_quick.csv")
    dsn = args.dsn or (payment.DSN_TMPFS if args.storage == "tmpfs" else payment.DSN_DISK)
    out = args.out or os.path.join(payment.HERE, f"results_{args.storage}_{args.cc}{suffix}.csv")
    fields = mix.FIELDS if is_mix else FIELDS

    done = set()
    if os.path.exists(out):
        with open(out, newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != fields:
                sys.exit(f"ABORT: {out} has other columns than this sweep writes (older code or "
                         f"other workload); move it aside or choose another --out.")
            if args.resume:
                done = {(r["rep"], r["arm"], r["warehouses"], r["clients"], r["district_layout"])
                        for r in reader}

    preflight(dsn)
    settings = payment.server_settings(dsn)
    calibration = payment.load_calibration(args.calibration)
    stmts_out = args.stmts_out or os.path.splitext(out)[0] + "_stmts.jsonl"
    arm_set = (payment.coarsening_arms() if args.coarsen else
               payment.e3_arms() if args.contiguous else args.arms.split(","))
    unknown = [a for a in arm_set if not (a in mix.ARMS if is_mix else payment.is_arm(a))]
    assert not unknown, f"unknown arm(s): {unknown}"
    n_runs = len(args.warehouses) * len(args.clients) * len(arm_set) * args.reps
    est_min = n_runs * (args.duration_s + args.warmup_s + 3) / 60
    print(f"workload={args.workload} storage={args.storage} cc={args.cc} settings={settings}")
    print(f"{n_runs} runs, about {est_min:.0f} min; writing {out}")

    new_file = not os.path.exists(out)
    with open(out, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            writer.writeheader()
        def rep_done(n_wh, rep):
            return all((str(rep), arm, str(n_wh), str(c), args.district) in done
                       for c in args.clients for arm in arm_set)

        def full_check(n_wh, when):
            bad = {k: v for k, v in mix.full_check(dsn).items() if v}
            print(f"wh={n_wh} full consistency check ({when}): {bad or 'OK'}", flush=True)
            if bad:
                sys.exit(f"ABORT: consistency violated ({when}, {n_wh} warehouses): {bad}")

        for n_wh in args.warehouses:
            if all(rep_done(n_wh, rep) for rep in range(args.reps)):
                continue                                  # resumed: block finished, no reload
            if not is_mix:
                payment.setup(dsn, n_wh, args.district)
                if args.prewarm_s > 0:
                    # discarded; the arm with the most round trips per second, at the most clients
                    arm = "B-inc/chopped" if "B-inc/chopped" in arm_set else arm_set[0]
                    w = payment.run_cell(dsn, arm, n_wh, max(args.clients), args.prewarm_s,
                                         args.warmup_s, seed=10**6, cc=args.cc, procs=args.procs,
                                         client_cpus=args.client_cpus, timing_every=0,
                                         pg_cpus=args.pg_cpus)
                    print(f"wh={n_wh} prewarm {arm} c={max(args.clients)} "
                          f"{w['throughput_pps']:8.1f} pps (discarded)  {w['status']}", flush=True)
            loaded = False
            for rep in range(args.reps):
                if is_mix:
                    if rep_done(n_wh, rep):
                        continue
                    if loaded and args.mix_check == "light":
                        full_check(n_wh, f"before reloading for rep {rep}")
                    mix.setup(dsn, n_wh, args.district)   # fresh database every rep
                    loaded = True
                for clients in args.clients:
                    arms = list(arm_set)
                    random.Random(f"{n_wh}-{rep}-{clients}").shuffle(arms)
                    for arm in arms:
                        if (str(rep), arm, str(n_wh), str(clients), args.district) in done:
                            continue
                        if is_mix:
                            row = mix.run_cell(dsn, arm, n_wh, clients, args.duration_s,
                                               args.warmup_s, seed=rep, procs=args.procs,
                                               client_cpus=args.client_cpus,
                                               check=args.mix_check)
                        else:
                            row = payment.run_cell(dsn, arm, n_wh, clients, args.duration_s,
                                                   args.warmup_s, seed=rep, cc=args.cc,
                                                   procs=args.procs,
                                                   client_cpus=args.client_cpus,
                                                   timing_every=args.timing_every,
                                                   calibration=calibration,
                                                   pg_cpus=args.pg_cpus)
                            stmts = row.pop("_stmts")
                            with open(stmts_out, "a", encoding="utf-8") as sf:
                                sf.write(json.dumps({
                                    "code_version": row["code_version"],
                                    "experiment": args.experiment, "rep": rep, "arm": arm,
                                    "warehouses": n_wh, "clients": clients,
                                    "timed_payments": row["timed_payments"],
                                    "stmts": stmts}) + "\n")
                            row["experiment"] = args.experiment
                        row.update(storage=args.storage, rep=rep,
                                   fsync=settings["fsync"],
                                   synchronous_commit=settings["synchronous_commit"],
                                   server_version=settings["server_version"],
                                   pg_cpus=args.pg_cpus)
                        writer.writerow(row)
                        f.flush()
                        if is_mix:
                            print(f"wh={n_wh} c={clients:>2} rep={rep} {arm:16s} "
                                  f"{row['throughput_tps']:8.1f} tps  "
                                  f"payment={row['tps_Payment']:7.1f}/s  "
                                  f"deadlocks={row['deadlocks']}  check={row['check_s']:.1f}s  "
                                  f"{row['status']}")
                        else:
                            print(f"wh={n_wh} c={clients:>2} rep={rep} {arm:16s} "
                                  f"{row['throughput_pps']:8.1f} pps  "
                                  f"retries/pay={row['retries_per_payment']:.3f}  "
                                  f"{row['status']}")
            if is_mix and loaded and args.mix_check == "light":
                full_check(n_wh, "end of block")


if __name__ == "__main__":
    main()

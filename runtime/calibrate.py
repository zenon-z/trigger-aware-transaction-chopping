"""
Statement baselines for the lock-wait estimate (MODEL.md R22), from the sidecar files sweep.py
writes (<out>_stmts.jsonl):

  python calibrate.py results_server_calib_stmts.jsonl --out calibration.json

- "hi" baseline: runs with 1 client (uncontended, no CPU load). Statement time above it is lock
  wait plus CPU queueing, so the estimate is an upper bound.
- "lo" baselines, one per client count c: runs with c clients on c warehouses (every client on
  its own home warehouse, so almost no lock conflicts, but at least the CPU load of c clients on
  1 warehouse). Statement time above it underestimates lock wait: a lower bound.

The baseline of a statement key is its mean time over all matching runs (total time / count);
payment.lock_wait_ms subtracts means, which keeps the estimate of the mean lock wait unbiased.
Runs with other shapes (e.g. 1 warehouse, 32 clients) are ignored. Only rows of one code version
may be combined.
"""
import argparse
import json
import sys
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl", nargs="+")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    acc = {"hi": defaultdict(lambda: [0, 0.0]), "lo": defaultdict(lambda: defaultdict(lambda: [0, 0.0]))}
    versions, runs = set(), defaultdict(int)
    for path in args.jsonl:
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r["clients"] == 1:
                    target, label = acc["hi"], "hi"
                elif r["clients"] == r["warehouses"]:
                    target, label = acc["lo"][r["clients"]], f"lo{r['clients']}"
                else:
                    continue
                versions.add(r["code_version"])
                runs[label] += 1
                for key, (n, s) in r["stmts"].items():
                    target[key][0] += n
                    target[key][1] += s
    if len(versions) > 1:
        sys.exit(f"ABORT: sidecars from several code versions {sorted(versions)}")
    if not acc["hi"]:
        sys.exit("ABORT: no 1-client runs (the hi baseline) in the input")
    mean = lambda d: {k: s / n for k, (n, s) in sorted(d.items()) if n}
    cal = {"code_version": versions.pop(), "runs": dict(runs),
           "hi": mean(acc["hi"]), "lo": {str(c): mean(d) for c, d in sorted(acc["lo"].items())}}
    for c, base in cal["lo"].items():
        missing = sorted(set(cal["hi"]) - set(base))
        if missing:
            print(f"WARNING: lo baseline for {c} clients lacks {missing}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cal, f, indent=1)
    keys = sorted(cal["hi"])
    print(f"{'statement key':36s} {'hi ms':>8s} " + " ".join(f"{'lo' + c:>8s}" for c in cal["lo"]))
    for k in keys:
        print(f"{k:36s} {cal['hi'][k] * 1000:8.3f} "
              + " ".join(f"{b[k] * 1000:8.3f}" if k in b else f"{'-':>8s}"
                         for b in cal["lo"].values()))
    print(f"wrote {args.out} from {dict(runs)} runs")


if __name__ == "__main__":
    main()

# Transaction Chopping with Triggers: Code and Data

This repository contains the code, the measured data and the figures for the evaluation of the paper *Transaction Chopping with Triggers* (Zenon Zacouris, Maribel Acosta).

A **CPR (Consistency Preservation Rule)** keeps data that is duplicated across models consistent. A write in one model triggers writes, and possibly reads, in another. The paper extends Shasha et al.'s transaction chopping to this setting. **CPR-aware FineChop** tests conflicts on *projections*: an operation together with everything it triggers.

The repository has two parts:

1. **Static analysis** (`chop.py`): TPC-C templates, CPR scenarios, FineChop and CPR-aware FineChop. It computes which choppings survive once CPRs are taken into account.
2. **Runtime experiment** (`runtime/`): TPC-C Payment on PostgreSQL. The `w_ytd` CPR is implemented as a trigger, either incremental or recomputing, and each trigger runs chopped and unchopped. The experiment measures throughput, latency, warehouse-lock hold time and where a Payment's time goes.

## Repository layout

```
chop.py                     static analysis (templates, CPR scenarios, FineChop, tables)
MODEL.md                    design log: every modeling and implementation decision, with reasons
runtime/
  schema.sql                TPC-C schema and loader, the two CPR triggers, consistency checks
  payment.py                runs one measurement (one arm, warehouses, clients); pieces come from chop.py
  sweep.py                  runs a grid of measurements, one CSV row per run, resumable
  run_server.sh             the paper's server campaign (checks, E1, E2, E3, figures)
  topology.py               proposes the core split for Postgres and clients (writes server.env)
  calibrate.py, checks.py   lock-wait baselines and verdicts of the pre-campaign checks
  summarize.py, plot.py     quick summaries of result CSVs
  paperstyle.py             figure style (IEEEtran column widths, fonts)
  paper_figs.py             paper figures from E1/E2/E3 CSVs
  plot_fig12_predictors.py  the E3 figure (throughput of every contiguous partition)
  mix.py                    full TPC-C mix runner (imported by sweep.py; not used in the paper)
  docker-compose*.yml       Postgres containers (local, and the server settings for reference)
  server.env.example        the core split used for the paper's runs
data/                       measured results of the paper's campaign (see "Data")
figures/                    the figures as they appear in the paper (PDF)
make_figures.sh             regenerates figures/ and the static tables from data/
```

## Requirements

- Python 3.12 with the packages in `requirements.txt` (`pip install -r requirements.txt`).
- Only for running new experiments: Docker, and Linux for the server campaign (core pinning, `/proc/stat`).

## Reproducing the paper's results from the included data (minutes, no database)

```
pip install -r requirements.txt
./make_figures.sh               # or run the three commands inside it by hand
```

This writes:
- `out/static_tables.txt`, the static analysis tables (`python chop.py`). Use `python chop.py -v` for piece lists and rollback hoisting.
- `out/figures/`: every figure, regenerated from `data/`. The ones in the paper are listed below. Regenerating them gives files identical to `figures/`.

| Paper figure | Files | Data |
|---|---|---|
| Throughput of the four arms | `throughput_wh1`, `throughput_c32`, legend `legend_arms` | E1, E2 |
| Warehouse-lock hold time (median) | `hold_wh1`, `hold_c32` | E1, E2 |
| Where a Payment's time goes | `decomp_wh1_c8`, `decomp_wh1_c32`, `decomp_wh1_c64`, `decomp_wh8_c32`, legend `legend_decomp` | E1, E2 |
| Median and p95 latency | `latency50_wh1`, `latency_wh1` | E1 |
| Throughput of every contiguous partition | `fig12_minmax_{pieces,ops}_{8c,32c}`, legend `legend_fig12_minmax` | E3 |

`paper_figs.py` also writes further panels that the paper doesn't use.

## Running the experiments

### Quick local check (Docker, any OS)

```
cd runtime
docker compose up -d pg-tmpfs              # Postgres 16.15 on port 15433, data on tmpfs
python payment.py --plan                   # pieces and pre-locks of every arm, no database needed
python payment.py --plan --contiguous      # every E3 arm
python sweep.py --storage tmpfs --quick    # smoke test, about 3 minutes
```

The smoke test passes when every row has `status = OK` with zero deadlocks and zero consistency violations.

### The paper's campaign (Linux server, about 22 hours)

The paper's runs used a server with 48 hardware threads and 566 GB of RAM. Client and database ran on the same machine, on separate cores.

```
cd runtime
./run_server.sh topology    # proposes the core split, writes server.env; check it
./run_server.sh checks      # ~2.5 h: smoke test, lock-wait calibration, timing overhead, merge, lock log
./run_server.sh campaign    # ~20 h: drift check, E1, E2, drift, E3, drift, figures
```

`run_server.sh` uses `~/ttc-venv/bin/python` if that exists, otherwise `python3`; set `PY=...` to override. Each cell runs on a fresh Postgres container (`postgres:16.15`, host networking, data on tmpfs). The script refuses to start while another benchmark client or another container is running.

| Experiment | Grid | Arms |
|---|---|---|
| E1 | 1 warehouse × 1, 2, 4, 8, 16, 32, 64 clients | 4 arms + each trigger's unchopped arm without the customer-update merge (`@nomerge`, MODEL.md R15) |
| E2 | 2, 4, 8 warehouses × 32 clients (1 warehouse comes from E1) | as E1 |
| E3 | 1 warehouse × 8, 32, 64 clients | all 48 contiguous partitions of both triggers' finest choppings (MODEL.md R21) |

Every experiment uses 10 repetitions. Each run consists of a 2 s warm-up and a 30 s measurement, and each fresh instance first gets a discarded 30 s run. The arms of a repetition are interleaved in a shuffled order.

## Execution protocol (summary; details in MODEL.md F6, R1–R3)

- **The four arms:** {incremental trigger, recomputing trigger} × {chopped, unchopped}.
  - Each trigger is compared with its own unchopped baseline.
  - The chopped arms execute exactly the pieces `chop.py` computes. Each arm is validated before it runs.
- **Concurrency control:** strict two-phase locking on rows, implemented as READ COMMITTED plus explicit row locks held until the piece commits.
  - Updates lock their row exclusively.
  - The recomputing trigger reads the district totals with `FOR SHARE`.
  - Reads of attributes no transaction writes take no lock.
- **Pre-lock:** before the statement that fires the CPR, the client locks the rows the CPR writes (the warehouse row) in the same round trip.
  - Every Payment thus locks warehouse, then district, then customer.
- **Pieces:** each piece is one transaction.
  - A piece aborted by a deadlock or serialization failure is retried until it commits. None occurred in the paper's runs.
- **Consistency:** after every run, TPC-C consistency conditions 1, 8 and 9 and the customer totals are checked against History.
- **Database:** PostgreSQL 16.15 with `fsync` and `synchronous_commit` on, data directory on tmpfs (in memory).

## Data

`data/` holds the raw results of the paper's campaign, one CSV row per run (10 repetitions per arm and cell):

| File | Content |
|---|---|
| `results_server_e1.csv`, `_e2.csv`, `_e3.csv` | E1, E2, E3 |
| `results_server_e*_stmts.jsonl` | per run: summed statement times per statement kind (input of the lock-wait estimate) |
| `calibration.json` | lock-wait baselines from the calibration runs |
| `checks/` | pre-campaign checks (smoke test, calibration, timing overhead) and the drift checks at the start, middle and end of the campaign |
| `static_tables.txt` | output of `python chop.py` |

Main columns:
- `arm`, `warehouses`, `clients`, `rep`, `pieces`
- `throughput_pps`: committed Payments per second
- `latency_{mean,p50,p95,p99}_ms`
- `wlock_hold_{p50,p95}_ms`: warehouse-lock hold per Payment
- `wlock_wait_*`
- `t_*_ms`: time breakdown, mean per Payment
- `sampler_lockwait_ms`: mean lock wait per Payment, from sampling `pg_stat_activity`
- `deadlocks`, `retries_per_payment`
- `cc*_violations`: consistency checks
- `status`
- configuration columns: storage, durability settings, Postgres version, cores

All 2,040 rows of E1–E3 have `status = OK`, zero deadlocks, retries and consistency violations.

The `code_version` column holds the commit hash of the development repository the runs were made from. This repository contains the same experiment code. The only addition since those runs is one extra arm type (`reorder_arm` in `payment.py`), which the paper doesn't use. The figure scripts are newer.

## Citation

```
@inproceedings{zacouris-chopping-cpr,
  author = {Zenon Zacouris and Maribel Acosta},
  title  = {Transaction Chopping with Triggers},
  year   = {2026}
}
```

## License

Code: MIT (see `LICENSE`). Data in `data/` and figures in `figures/`: CC BY 4.0.

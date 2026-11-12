# Trigger-Aware-Finechop: Code and Data

Code, measured data and figures for the paper *Trigger-Aware-Finechop: Serializable Execution of Chopped Transactions in the Presence of Triggers* (Zenon Zacouris, Maribel Acosta).

**Trigger-Aware-Finechop** extends Shasha et al.'s FineChop so that a transaction's triggered operations are taken into account: conflicts are tested on an operation together with everything it triggers, and a triggered operation always stays in the piece of the operation that triggered it.

The repository has two parts:

1. **Static analysis** (`chop.py`): TPC-C transactions, trigger scenarios, FineChop and Trigger-Aware-Finechop. It computes the finest correct chopping of each transaction with and without triggers.
2. **Experiments** (`runtime/`): TPC-C Payment on PostgreSQL, with the warehouse year-to-date value (`w_ytd`) maintained by a trigger instead of by Payment itself. Two triggers:
   - **incremental:** `w_ytd += amount` whenever a district's `d_ytd` changes;
   - **recomputing:** `w_ytd = sum(d_ytd)` over the warehouse's districts.

   Each trigger runs with Payment chopped (Trigger-Aware-Finechop's finest chopping) and unchopped (one transaction). The experiments measure throughput, latency, how long the warehouse lock is held, and where a Payment's time goes.

## Naming in the code

The code predates the paper's final terminology:

| Paper | Code |
|---|---|
| trigger | CPR (`cpr(...)` in `chop.py`, `set_cpr` in `schema.sql`) |
| incremental trigger | `B-inc` |
| recomputing trigger | `B-rec` |
| variant (trigger × chopped/unchopped, or one chopping) | arm (column `arm`, option `--arms`), e.g. `B-inc/chopped` |
| Trigger-Aware-Finechop | CPR-aware FineChop (`fine_chop` on projections) |

## Repository layout

```
chop.py                     static analysis: transactions, triggers, FineChop, Trigger-Aware-Finechop, tables
MODEL.md                    design log: every modeling and implementation decision, with reasons
runtime/
  schema.sql                TPC-C schema and loader, the two triggers, consistency checks
  payment.py                runs one measurement (one variant, number of warehouses and clients);
                            the pieces come from chop.py
  sweep.py                  runs a set of measurements, one CSV row per run, resumable
  run_server.sh             the paper's experiments (checks, E1, E2, E3, figures)
  topology.py               proposes the core split for Postgres and clients (writes server.env)
  calibrate.py, checks.py   lock-wait baselines and verdicts of the checks before the experiments
  summarize.py, plot.py     quick summaries of result CSVs
  paperstyle.py             figure style (IEEEtran column widths, fonts)
  paper_figs.py             paper figures from the E1/E2/E3 results
  plot_fig12_predictors.py  the E3 figure (throughput of every contiguous partition)
  mix.py                    full TPC-C mix (imported by sweep.py; not used in the paper)
  docker-compose*.yml       Postgres containers (local, and the server settings for reference)
  server.env.example        the core split used for the paper's runs
data/                       measured results of the paper's experiments (see "Data")
figures/                    the figures as they appear in the paper (PDF)
make_figures.sh             regenerates the figures and the static tables from data/
```

## Requirements

- Python 3.12 with the packages in `requirements.txt` (`pip install -r requirements.txt`).
- Only for running new experiments: Docker, and Linux for the full experiments (core pinning, `/proc/stat`).

## Reproducing the paper's results from the included data (minutes, no database)

```
pip install -r requirements.txt
./make_figures.sh
```

This writes:
- `out/static_tables.txt`: the static analysis tables (`python chop.py`; `python chop.py -v` also lists the pieces).
- `out/figures/`: every figure, regenerated from `data/`. The ones in the paper are identical to `figures/`:

| Paper figure | Files | Data |
|---|---|---|
| Throughput | `throughput_wh1`, `throughput_c32`, legend `legend_arms` | E1, E2 |
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
python payment.py --plan                   # the pieces of every variant, no database needed
python payment.py --plan --contiguous      # every E3 partition
python sweep.py --storage tmpfs --quick    # smoke test, about 3 minutes
```

The smoke test passes when every row has `status = OK` with zero deadlocks and zero consistency violations.

### The paper's experiments (Linux server, about 22 hours)

The paper's runs used a server with 48 hardware threads and 566 GB of RAM; clients and database ran on the same machine, on separate cores.

```
cd runtime
./run_server.sh topology    # proposes the core split, writes server.env; check it
./run_server.sh checks      # ~2.5 h: smoke test, lock-wait calibration, timing overhead, merge, lock log
./run_server.sh campaign    # ~20 h: drift check, E1, E2, drift, E3, drift, figures
```

`run_server.sh` uses `~/ttc-venv/bin/python` if it exists, otherwise `python3`; set `PY=...` to override. Every combination of warehouses and clients runs on a fresh Postgres container (`postgres:16.15`, host networking, data on tmpfs). The script refuses to start while another benchmark client or container is running.

| Experiment | Warehouses × clients | Variants |
|---|---|---|
| E1 | 1 × 1, 2, 4, 8, 16, 32, 64 | each trigger chopped and unchopped, plus each trigger unchopped with Payment's two customer updates as separate statements (`@nomerge`, MODEL.md R15) |
| E2 | 2, 4, 8 × 32 (1 × 32 comes from E1) | as E1 |
| E3 | 1 × 8, 32, 64 | every contiguous partition of both triggers' finest choppings, 48 in total (MODEL.md R21) |

Every measurement is repeated 10 times: a 2 s warm-up, then 30 s measured. Each fresh instance first gets one discarded 30 s run, and the variants of a repetition run in shuffled order, interleaved.

## How Payment is executed (details in MODEL.md F6, R1–R3)

- **Concurrency control:** strict two-phase locking on rows, implemented as READ COMMITTED plus explicit row locks held until the piece commits.
  - Updates lock their row exclusively.
  - The recomputing trigger reads the district totals with `FOR SHARE`.
  - Reads of attributes that no transaction writes take no lock.
- **Lock order:** in the same round trip as the update that fires the trigger, the client first locks the row the trigger writes (the warehouse row). Every Payment therefore locks warehouse, then district, then customer.
- **Pieces:** each piece is one transaction. A piece aborted by a deadlock or serialization failure is retried until it commits; none occurred in the paper's runs.
- **Consistency:** after every run, TPC-C consistency conditions 1, 8 and 9 and the customer totals are checked against History.
- **Database:** PostgreSQL 16.15, `fsync` and `synchronous_commit` on, data directory on tmpfs (in memory).

## Data

`data/` holds the raw results of the paper's experiments, one CSV row per run:

| File | Content |
|---|---|
| `results_server_e1.csv`, `_e2.csv`, `_e3.csv` | E1, E2, E3 |
| `results_server_e*_stmts.jsonl` | per run: summed statement times per statement kind (input of the lock-wait estimate) |
| `calibration.json` | lock-wait baselines from the calibration runs |
| `checks/` | checks before the experiments (smoke test, calibration, timing overhead) and the drift checks at the start, middle and end |
| `static_tables.txt` | output of `python chop.py` |

Main columns:
- `arm` (the variant), `warehouses`, `clients`, `rep`, `pieces`
- `throughput_pps`: committed Payments per second
- `latency_{mean,p50,p95,p99}_ms`
- `wlock_hold_{p50,p95}_ms`: how long a Payment holds the warehouse lock
- `wlock_wait_*`: how long it waits for it
- `t_*_ms`: time breakdown, mean per Payment
- `sampler_lockwait_ms`: mean lock wait per Payment, from sampling `pg_stat_activity`
- `deadlocks`, `retries_per_payment`
- `cc*_violations`: consistency checks
- `status`
- configuration: storage, durability settings, Postgres version, cores

All 2,040 rows of E1–E3 have `status = OK` and zero deadlocks, retries and consistency violations.

The `code_version` column holds the commit hash of the development repository the runs were made from. This repository contains the same experiment code; the only addition since those runs is one more variant (`reorder_arm` in `payment.py`) that the paper doesn't use, and the figure scripts are newer.

## Citation

```
@inproceedings{zacouris-chopping-triggers,
  author = {Zenon Zacouris and Maribel Acosta},
  title  = {Trigger-Aware-Finechop: Serializable Execution of Chopped Transactions in the Presence of Triggers},
  year   = {2026}
}
```

## License

Code: MIT (see `LICENSE`). Data in `data/` and figures in `figures/`: CC BY 4.0.

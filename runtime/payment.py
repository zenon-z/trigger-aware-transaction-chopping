"""
Runtime check for scenario B: TPC-C Payment with the W_YTD CPR as a
Postgres trigger, incremental (B-inc) or recomputing (B-rec), each run chopped or unchopped.

- The pieces come from chop.py (Payment-only workload, scenario B-inc / B-rec), so the runtime
  executes exactly the chopping the analysis computed. Unchopped = one piece with every op.
- Every piece is its own transaction under strict 2PL on rows (default, MODEL.md F6): READ
  COMMITTED plus explicit row locks held to commit. UPDATEs lock their row, the B-rec trigger's
  read of d_ytd takes FOR SHARE, and reads of cells that are never written (w_name, d_name, the
  customer's name and id) take no lock (MODEL.md F6, changed Oct 14): no write can conflict with
  them, and the FOR KEY SHARE they took before never blocked a non-key update anyway, while it
  made Postgres combine all concurrent share holders of the warehouse row into large multixacts
  on every update of that row. The arm suffix "@keyshare" restores that old protocol for
  comparison. Serializable because every access is a key lookup or a range read over rows that
  are never inserted or deleted, with match columns never written. `--cc ssi` runs the pieces as
  SERIALIZABLE instead.
- Pre-lock rule (2PL, both triggers): before a statement that fires a CPR, the client locks
  every row that CPR writes (here the warehouse row, FOR NO KEY UPDATE), in the same piece and in
  the same round trip as that statement (a trigger takes its locks inside the statement, so a
  separate round trip would inflate the hold time of the hottest lock). The
  rule is derived from the CPR definitions in chop.py (prologue_for), so it is the same for every
  trigger. For B-rec it is what prevents deadlocks: every transaction holding a district row of a
  warehouse then holds that warehouse's row too, so the recompute's share locks on all districts
  cannot form a cycle. For B-inc it only takes the same lock one statement earlier. A BEFORE
  trigger cannot do this: Postgres locks the district row before BEFORE row triggers run. Order
  within a piece does not matter to the analysis.
- A Payment always runs to its last piece, also after the measurement window closes; it counts
  when its last piece commits. After each run, TPC-C consistency conditions 1, 8 and 9 and the
  customer totals are checked against History, which catches any piece that was lost.
- Warehouse-lock hold time per Payment (the quantity the analysis predicts): from the moment the
  pre-lock is granted to the commit of the piece holding it, without an extra round trip. The
  server notes the time right after the lock statement and, at the end of the same call, returns
  the time elapsed since (district update + trigger); the client adds the time from that response
  to the commit acknowledgement. The two one-way network delays (call response, commit ack)
  roughly cancel. Lock waits are not included: the clock starts when the lock is granted.
- A piece that fails with a deadlock (40P01) or serialization failure (40001) is resubmitted
  until it commits; later pieces wait for it (Shasha's execution rule 3). Retries are counted
  per piece and per cause.
- Payment follows spec order without the explicit w_ytd update (the CPR maintains it). The
  History insert is kept: it is in the contended piece. For a customer with bad credit (BC),
  pa5 also updates c_data in the same statement (clause 2.5.2.2).
- One statement per op, except that ops the spec performs as one SQL statement on one row run as
  that statement when they land in the same piece (MODEL.md R15): here pa5 and pa6 (customer),
  so only arms that keep them together (unchopped, coarsened) merge them. Order within a piece
  does not matter to the analysis, so this only saves the round trip the spec does not have.
- d_ytd lives in district_ytd (vertical split of District, MODEL.md R14). `setup(...,
  layout="joined")` restores the stock layout (district_ytd becomes a view on district) for the
  check that quantifies this choice; every row records the layout.
- Where the clients wait (MODEL.md R18): during the measurement window a sampler reads
  pg_stat_activity every 100 ms and records, as a fraction of the clients, how many wait for a
  lock in which statement (Payment's CPR call = warehouse pre-lock + district update; NewOrder's
  district update; mix.py's district table locks; customer; stock; other), how many hold an open
  transaction while the client works, and how many run in the server. A row-lock wait does not
  name its table in Postgres, so the statement is what identifies it. Per Payment, the wait for
  the warehouse lock is also timed server-side in the same call as the hold time
  (wlock_wait_*), which separates it from the rest of the CPR call.
- Payment inputs as in TPC-C clause 2.5.1.2 (MODEL.md R12): home warehouse per client, district
  uniform, 15% remote customers (other warehouse, any district; never with one warehouse), 60%
  lookups by last name (NURand(255, 0, 999)), 40% by id (NURand(1023, 1, 3000)). NURand and the
  last names follow clause 2.1.6 and 4.3.2.3, as in py-tpcc. The customer read `pc` runs in every
  Payment (by name: all matches, ordered by first name, the middle one is chosen, clause
  2.5.2.2), and the id it resolves is carried to pa5, pa6 and pa7, which never look the customer
  up again.

- Coarsening arms (`B-inc/k3`, ...): starting from FineChop's finest chopping, pieces are merged
  one step at a time down to a single piece, in the reverse of the order in which chopping adds
  value (MODEL.md R10):
    1. conflict-free reads first: two read pieces are merged with each other, then the read piece
       into the contended piece (they hold no contended lock; Shasha's rule of thumb);
    2. then free hot increments into the contended piece, coldest cell first by rows per
       warehouse (customer before district/warehouse), so the last steps give up the hottest
       locks;
  until one piece is left, which is the unchopped transaction. Every step is a valid chopping
  (coarsening a correct chopping is correct, Shasha's Lemma 2); after each merge the dependency
  graph is checked to be acyclic and the pieces are re-sorted into an executable order. `kN` is
  the chopping with N pieces; `--plan --coarsen` prints the whole chain.
- Contiguous arms (`B-inc/b1.2.3`, ...; server experiment E3, MODEL.md R21): every partition of
  FineChop's finest chopping, in the order chop.py emits it, into j contiguous blocks
  (2 <= j < k, C(k-1, j-1) of them per j); the name lists the block sizes. Ops run in the
  emitted order, pieces are never reordered. j = k is `/chopped`. j = 1 in the emitted order is
  `B-inc/b6` / `B-rec/b5` (reorder_arm); `/unchopped` is one piece in TPC-C's order, which for
  B-rec takes the warehouse lock before pa4 and pc instead of after them (MODEL.md R24).
- Every arm is checked before it runs (`validate_arm`): every op once, every finest piece inside
  one arm piece (a coarsening, correct by Shasha's Lemma 2), the piece dependency graph acyclic
  with every edge pointing forward in the listed order, and the issued statements in an order
  that respects every data-flow and ordering edge.
- Arm variants: "@keyshare" (protocol before Oct 14), "@nomerge" (pa5 and pa6 always as two
  statements, MODEL.md R15), "@notiming" (no per-statement timing, for the overhead check).
- Time breakdown per Payment (MODEL.md R22): chained client timestamps split the end-to-end time
  into statement time, commit time, the client's gaps within and between pieces, and retries;
  they add up to the end-to-end time by construction. Lock wait is estimated as the statement
  time above a calibrated baseline per statement (calibrate.py), as a bracket: against an
  uncontended baseline (1 client; upper bound, it also contains CPU queueing) and against a
  load-matched one (c clients on c warehouses; lower bound). Cross-checks: the exact
  warehouse-lock wait (wlock_wait_*), the sampler's mean lock wait per Payment by Little's law,
  and, with log_lock_waits on (service pg-tmpfs-locklog), the waits Postgres logs. Also per
  Payment: time to its first commit (ttfc) and to the commit of the piece holding the
  warehouse lock (tthc).
- Clients run as threads in one process by default. `--procs P` spreads them over P processes
  (for 128+ clients, so the client cannot become the bottleneck); `--client-cpus` pins the client
  processes to given cores (Linux only), away from the cores Postgres is pinned to.

Usage:
  python payment.py --plan                      # print the pieces of each arm, no database
  python payment.py --plan --coarsen            # print the coarsening chain of both triggers
  python payment.py --plan --contiguous         # print E3's contiguous arms and what they issue
  python payment.py --plan --arm B-inc/b1.1.4   # pieces and issued statements of one arm
  python payment.py --setup --warehouses 2      # load schema + data
  python payment.py --arm B-inc/chopped --warehouses 2 --clients 8
"""
import argparse
import functools
import itertools
import json
import math
import multiprocessing as mp
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import psycopg2
import psycopg2.errors

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import chop  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DSN_TMPFS = "host=127.0.0.1 port=15433 dbname=cpr user=cpr password=cpr"
DSN_DISK = "host=127.0.0.1 port=15434 dbname=cpr user=cpr password=cpr"
N_CUST = 3000          # customers per district (TPC-C clause 1.2)
RETRYABLE = (psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected)
# NURand constants (TPC-C clause 2.1.6). C_LOAD is used for loading last names, C_RUN for the run;
# their difference must satisfy clause 2.1.6.1 (checked below, as py-tpcc's validCRun does).
C_LOAD, C_RUN, C_ID = 157, 223, 259
assert 65 <= abs(C_RUN - C_LOAD) <= 119 and abs(C_RUN - C_LOAD) not in (96, 112)
SYLLABLES = ["BAR", "OUGHT", "ABLE", "PRI", "PRES", "ESE", "ANTI", "CALLY", "ATION", "EING"]


def nurand(rng, a, x, y, c):
    """NURand(A, x, y) (TPC-C clause 2.1.6), as in py-tpcc."""
    return (((rng.randint(0, a) | rng.randint(x, y)) + c) % (y - x + 1)) + x


def last_name(n):
    """Last name for 0..999 (TPC-C clause 4.3.2.3); schema.sql's last_name() is the same."""
    return SYLLABLES[n // 100] + SYLLABLES[n // 10 % 10] + SYLLABLES[n % 10]


def payment_params(rng, home_w, n_wh):
    """Inputs of one Payment (TPC-C clause 2.5.1.2)."""
    d = rng.randint(1, 10)
    if n_wh == 1 or rng.randint(1, 100) <= 85:
        cw, cd = home_w, d
    else:
        cw, cd = rng.choice([w for w in range(1, n_wh + 1) if w != home_w]), rng.randint(1, 10)
    by_name = rng.randint(1, 100) <= 60
    return {"w": home_w, "d": d, "cw": cw, "cd": cd,
            "lookup": "name" if by_name else "id",
            "c_last": last_name(nurand(rng, 255, 0, min(999, N_CUST - 1), C_RUN)) if by_name else None,
            "c": None if by_name else nurand(rng, 1023, 1, N_CUST, C_ID),
            "amt": round(rng.uniform(1.0, 5000.0), 2)}

# One SQL statement per chop.py op id. Statements with a result store it under the given key,
# which later statements use (chop.py's `uses` edges: pa7 uses pa2 and pa4; pa5, pa6 and pa7 use
# the customer id from pc). pc has one statement per lookup kind, chosen by the Payment's input.
CUSTOMER_SQL = "SELECT c_id, c_first, c_last FROM customer WHERE c_w_id = %(cw)s AND c_d_id = %(cd)s"
# Bad credit: prepend the payment to c_data, keep 500 characters (clause 2.5.2.2).
C_DATA_SET = ("c_data = CASE WHEN c_credit = 'BC' THEN left(concat_ws(' ', c_id, c_d_id, c_w_id, "
              "%(d)s, %(w)s, %(amt)s, '|', c_data), 500) ELSE c_data END")
OP_SQL = {
    "pa2": ("SELECT w_name FROM warehouse WHERE w_id = %(w)s", "w_name"),
    "pa3": ("UPDATE district_ytd SET d_ytd = d_ytd + %(amt)s "
            "WHERE d_w_id = %(w)s AND d_id = %(d)s", None),
    "pa4": ("SELECT d_name FROM district WHERE d_w_id = %(w)s AND d_id = %(d)s", "d_name"),
    "pc": ({"id": CUSTOMER_SQL + " AND c_id = %(c)s",
            "name": CUSTOMER_SQL + " AND c_last = %(c_last)s ORDER BY c_first"}, "c_id"),
    "pa5": ("UPDATE customer SET c_balance = c_balance - %(amt)s, " + C_DATA_SET +
            " WHERE c_w_id = %(cw)s AND c_d_id = %(cd)s AND c_id = %(c_id)s RETURNING c_balance",
            "c_balance"),
    "pa6": ("UPDATE customer SET c_ytd_payment = c_ytd_payment + %(amt)s, "
            "c_payment_cnt = c_payment_cnt + 1 "
            "WHERE c_w_id = %(cw)s AND c_d_id = %(cd)s AND c_id = %(c_id)s", None),
    "pa7": ("INSERT INTO history (h_c_id, h_c_d_id, h_c_w_id, h_d_id, h_w_id, h_date, h_amount, "
            "h_data) VALUES (%(c_id)s, %(cd)s, %(cw)s, %(d)s, %(w)s, now(), %(amt)s, "
            "%(w_name)s || '    ' || %(d_name)s)", None),
}

# Ops the spec performs as one statement on one row: run as that statement when they share a
# piece (MODEL.md R15). Key: the ops in template order; value: (SQL, result key).
MERGED_SQL = {
    ("pa5", "pa6"): ("UPDATE customer SET c_balance = c_balance - %(amt)s, "
                     "c_ytd_payment = c_ytd_payment + %(amt)s, c_payment_cnt = c_payment_cnt + 1, "
                     + C_DATA_SET + " WHERE c_w_id = %(cw)s AND c_d_id = %(cd)s "
                     "AND c_id = %(c_id)s RETURNING c_balance", "c_balance"),
}
# R15 for every arm without "@nomerge". Set to False if the server's merge check shows that the
# one statement the merged arms save changes throughput by about 10% or more (MODEL.md R15).
MERGE_SAME_ROW = True

# Strict 2PL: reads of never-written cells take no lock (MODEL.md F6, see module docstring).
OP_SQL_2PL = dict(OP_SQL)
# The protocol before Oct 14, for comparison runs (arm suffix "@keyshare"): static reads took
# FOR KEY SHARE; for pc by name that locked every matching customer row.
OP_SQL_KEYSHARE = dict(OP_SQL,
                       pa2=(OP_SQL["pa2"][0] + " FOR KEY SHARE", "w_name"),
                       pa4=(OP_SQL["pa4"][0] + " FOR KEY SHARE", "d_name"),
                       pc=({k: v + " FOR KEY SHARE" for k, v in OP_SQL["pc"][0].items()}, "c_id"))
# Row lock per relation a CPR may write, taken before the CPR's triggering statement (2PL only).
WRITE_LOCK_SQL = {"Warehouse": "SELECT 1 FROM warehouse WHERE w_id = %(w)s FOR NO KEY UPDATE"}
CC = {  # concurrency control -> (isolation level, statements, B-rec trigger variant)
    "2pl": ("READ COMMITTED", OP_SQL_2PL, "rec_lock"),
    "ssi": ("SERIALIZABLE", OP_SQL, "rec"),
}

ARMS = ["B-inc/unchopped", "B-inc/chopped", "B-rec/unchopped", "B-rec/chopped"]
SCENARIOS = ["B-inc", "B-rec"]


def coarsening_chain(scenario):
    """Choppings from FineChop's finest down to one piece: [(pieces, kinds), ...], finest first.
    kinds per piece: 'contended', 'inc' (a free commutative increment, incl. triggered ones,
    i.e. a hot lock at runtime) or 'read' (conflict-free, no increment). Merge order: MODEL.md R10
    and the module docstring."""
    sc = chop.TPCC_SCENARIOS[scenario]
    payment = sc["templates"]["Payment"]
    ops = payment["ops"]
    byid = {o["id"]: o for o in ops}
    pos = {o["id"]: k for k, o in enumerate(ops)}
    finest = chop.chop_config({"Payment": payment}, sc["cprs"])["Payment"][0]

    def increments(ids):
        return [x for i in ids for x in chop.projection(byid[i], sc["cprs"])
                if x["kind"] == "U" and x["comm"]]

    def kind(ids, hot):
        return "contended" if hot else "inc" if increments(ids) else "read"

    def heat_rows(ids):
        """Rows per warehouse of the hottest incremented cell (fewer rows = hotter lock)."""
        return min(chop.TPCC_ROWS.get(x["rel"], float("inf")) for x in increments(ids))

    def ordered(pieces):
        """Sort pieces into an executable order; refuse a merge that created a cycle."""
        D = chop.dependency_graph(pieces, ops, sc["cprs"])
        assert chop.nx.is_directed_acyclic_graph(D), "merge created a dependency cycle"
        order = chop.nx.lexicographical_topological_sort(
            D, key=lambda k: min(pos[x] for x in pieces[k]))
        return [sorted(pieces[k], key=pos.get) for k in order]

    def merge(pieces, kinds, a, b):
        merged = sorted(pieces[a] + pieces[b], key=pos.get)
        new_kind = ("contended" if "contended" in (kinds[a], kinds[b]) else
                    "inc" if "inc" in (kinds[a], kinds[b]) else "read")
        rest = [(p, k) for j, (p, k) in enumerate(zip(pieces, kinds)) if j not in (a, b)]
        new = ordered([p for p, _ in rest] + [merged])
        kind_of = {tuple(p): k for p, k in rest}
        kind_of[tuple(merged)] = new_kind
        return new, [kind_of[tuple(p)] for p in new]

    pieces = [sorted(ids, key=pos.get) for ids, _, _ in finest]
    kinds = [kind(ids, hot) for ids, hot, _ in finest]
    chain = [([list(p) for p in pieces], list(kinds))]
    while len(pieces) > 1:
        reads = [j for j, k in enumerate(kinds) if k == "read"]
        incs = [j for j, k in enumerate(kinds) if k == "inc"]
        cont = [j for j, k in enumerate(kinds) if k == "contended"]
        if len(reads) >= 2:                       # 1a. reads with each other
            a, b = reads[0], reads[1]
        elif reads:                               # 1b. the read piece into the contended piece
            a, b = reads[0], (cont or incs)[0]
        elif incs:                                # 2. coldest free increment into contended
            a = max(incs, key=lambda j: (heat_rows(pieces[j]), -j))
            b = (cont or [j for j in incs if j != a])[0]
        else:                                     # only contended pieces left (not expected)
            a, b = 0, 1
        pieces, kinds = merge(pieces, kinds, a, b)
        chain.append(([list(p) for p in pieces], list(kinds)))
    return chain


# Hand-chosen coarsenings for the merge-target experiment (earlier local experiment, not in the paper).
# Not FineChop output: each is checked to be a coarsening of FineChop's finest chopping whose
# pieces and in-piece order respect every data-flow and ordering edge (check_explicit). Pieces
# and their ops run exactly in the order listed.
EXPLICIT_ARMS = {
    # hot increment alone, first (position-matched to the merged arms, whose hot piece must run
    # first because pa5 and pa6 use pc)
    "B-inc/x-hotalone-first": [["pa3"], ["pa2", "pa4", "pc", "pa5", "pa6", "pa7"]],
    # the conflict-free reads merged into the hot piece, template order: the warehouse lock is
    # taken at pa3 and held across pa4 and pc
    "B-inc/x-hotmerged": [["pa2", "pa3", "pa4", "pc"], ["pa5", "pa6", "pa7"]],
    # same pieces, pa3 last in its piece: the lock is held across nothing
    "B-inc/x-hotmerged-last": [["pa2", "pa4", "pc", "pa3"], ["pa5", "pa6", "pa7"]],
    # control: as above, but pa2 moved to the other piece, so the hot piece holds no share lock on
    # the row it updates (multixact experiment, run with the old FOR KEY SHARE protocol; under the
    # current F6, pa2 takes no lock and this arm differs from the one above only in order)
    "B-inc/x-hotmerged-last-nowh": [["pa4", "pc", "pa3"], ["pa2", "pa5", "pa6", "pa7"]],
}


def coarsening_arms():
    """All kN arms, finest first, for both triggers."""
    return [f"{sc}/k{len(p)}" for sc in SCENARIOS for p, _ in coarsening_chain(sc)]


@functools.lru_cache(maxsize=None)
def finest_pieces(scenario):
    """FineChop's finest chopping of Payment, pieces in the order chop.py emits them."""
    sc = chop.TPCC_SCENARIOS[scenario]
    payment = sc["templates"]["Payment"]
    return tuple(tuple(ids) for ids, _, _ in
                 chop.chop_config({"Payment": payment}, sc["cprs"])["Payment"][0])


@functools.lru_cache(maxsize=None)
def contiguous_arms(scenario):
    """All partitions of the finest chopping into 2 <= j < k contiguous blocks, j descending.
    Arm name: block sizes, e.g. B-inc/b1.2.3 (the first block is the first finest piece, ...)."""
    k = len(finest_pieces(scenario))
    out = []
    for j in range(k - 1, 1, -1):
        arms = []
        for cuts in itertools.combinations(range(1, k), j - 1):
            bounds = (0, *cuts, k)
            arms.append(f"{scenario}/b" + ".".join(str(b - a) for a, b in zip(bounds, bounds[1:])))
        assert len(arms) == math.comb(k - 1, j - 1), (scenario, j, len(arms))
        out += arms
    return tuple(out)


def contiguous_pieces(scenario, sizes):
    """The pieces of a contiguous arm: consecutive finest pieces joined, ops in emitted order."""
    fin = finest_pieces(scenario)
    assert all(s > 0 for s in sizes) and sum(sizes) == len(fin), f"bad block sizes {sizes}"
    out, i = [], 0
    for s in sizes:
        out.append([x for p in fin[i:i + s] for x in p])
        i += s
    return out


def reorder_arm(scenario):
    """The whole Payment as one transaction, ops in chop.py's emitted order (`B-inc/b6`,
    `B-rec/b5`): the j = 1 end of E3's contiguous partitions. `/unchopped` runs the TPC-C order
    instead, so the two differ only in order (stage `reorder`, MODEL.md R24)."""
    return f"{scenario}/b{len(finest_pieces(scenario))}"


def e3_arms():
    """E3's arm set: per trigger the unchopped and finest arms plus every contiguous partition."""
    return [a for sc in SCENARIOS
            for a in (f"{sc}/unchopped", f"{sc}/chopped", *contiguous_arms(sc))]


# Arm variants, at most one per arm:
#   "@keyshare": the protocol before Oct 14 (static reads take FOR KEY SHARE), so old and current
#                protocol can run interleaved. (Experiment (a) on Oct 14 used the inverse suffix
#                "@pa2plain" under the old default; see its code_version 0c2b9c4.)
#   "@nomerge":  pa5 and pa6 always as two statements, also when they share a piece (R15 check)
#   "@notiming": no per-statement timing (overhead check of the time breakdown)
VARIANTS = ("@keyshare", "@nomerge", "@notiming")


def base_arm(arm):
    return arm.split("@")[0]


def arm_variant(arm):
    return "@" + arm.split("@", 1)[1] if "@" in arm else ""


def merged_for(arm):
    """Ops issued as one statement when they share a piece (MODEL.md R15), for this arm."""
    return {} if arm_variant(arm) == "@nomerge" or not MERGE_SAME_ROW else MERGED_SQL


def is_arm(arm):
    if "@" in arm and arm_variant(arm) not in VARIANTS:
        return False
    arm = base_arm(arm)
    if arm in ARMS or arm in coarsening_arms() or arm in EXPLICIT_ARMS:
        return True
    scenario = arm.split("/")[0]
    return scenario in SCENARIOS and (arm in contiguous_arms(scenario)
                                      or arm == reorder_arm(scenario))


def raw_pieces(arm, cc="2pl"):
    """(trigger variant, pieces in execution order) for an arm, unchecked."""
    arm = base_arm(arm)
    scenario, mode = arm.split("/")
    sc = chop.TPCC_SCENARIOS[scenario]
    payment = sc["templates"]["Payment"]
    variant = {"B-inc": "inc", "B-rec": CC[cc][2]}[scenario]
    if mode == "unchopped":
        return variant, [[o["id"] for o in payment["ops"]]]
    if arm in EXPLICIT_ARMS:
        return variant, [list(p) for p in EXPLICIT_ARMS[arm]]
    if mode.startswith("k"):
        for pieces, _ in coarsening_chain(scenario):
            if len(pieces) == int(mode[1:]):
                return variant, pieces
        raise ValueError(f"no chopping with {mode[1:]} pieces for {scenario}")
    if mode.startswith("b"):
        return variant, contiguous_pieces(scenario, [int(s) for s in mode[1:].split(".")])
    return variant, [list(p) for p in finest_pieces(scenario)]


@functools.lru_cache(maxsize=None)
def validate_arm(arm):
    """Assert that an arm is a valid, executable coarsening of FineChop's finest chopping: every
    op once; every finest piece inside one arm piece (coarsening a correct chopping is correct,
    Shasha's Lemma 2); the piece dependency graph acyclic with every edge pointing forward in the
    listed order; and the statements as issued (after merging, R15) after every op they use or
    must follow."""
    scenario = base_arm(arm).split("/")[0]
    sc = chop.TPCC_SCENARIOS[scenario]
    payment = sc["templates"]["Payment"]
    byid = {o["id"]: o for o in payment["ops"]}
    _, pieces = raw_pieces(arm)
    ops = [x for p in pieces for x in p]
    assert sorted(ops) == sorted(byid), f"{arm}: not every op exactly once"
    for ids in finest_pieces(scenario):
        assert any(set(ids) <= set(p) for p in pieces), f"{arm}: splits finest piece {ids}"
    D = chop.dependency_graph([set(p) for p in pieces], payment["ops"], sc["cprs"])
    assert chop.nx.is_directed_acyclic_graph(D), f"{arm}: dependency cycle"
    assert all(i < j for i, j in D.edges), f"{arm}: piece order violates the dependency graph"
    seen = set()
    for p in pieces:
        for op_ids, *_ in piece_statements(p, OP_SQL, prologue_for(arm), merged_for(arm)):
            for o_id in op_ids:
                missing = chop.deps(byid[o_id]) - seen
                assert not missing, f"{arm}: {o_id} is issued before {missing}"
            seen.update(op_ids)
    return True


def pieces_for(arm, cc="2pl"):
    """(trigger variant, pieces in execution order) for an arm. A piece is a list of op ids.
    Every arm is validated (validate_arm) before it is returned."""
    validate_arm(arm)
    return raw_pieces(arm, cc)


def prologue_for(arm, cc="2pl"):
    """Pre-lock rule: {op id: lock statements to run right before that op}. Before an op that
    fires a CPR, lock every row the CPR writes. Derived from chop.py's CPRs for the arm."""
    if cc != "2pl":
        return {}
    sc = chop.TPCC_SCENARIOS[arm.split("/")[0]]
    out = {}
    for o in sc["templates"]["Payment"]["ops"]:
        if not chop.writes(o):
            continue
        for c in sc["cprs"]:
            if c["rel"] == o["rel"] and c["attrs"] & o["attrs"]:
                for t in c["triggered"]:
                    if chop.writes(t):
                        out.setdefault(o["id"], []).append(WRITE_LOCK_SQL[t["rel"]])
    return out


def piece_label(piece):
    return "+".join(piece)


def piece_statements(piece, op_sql, prologue, merged=MERGED_SQL):
    """The statements of a piece, in order: [(op ids, sql, result key, pre-lock statements)].
    Ops listed together in `merged` become one statement if all of them are in the piece."""
    out, done = [], set()
    for op_id in piece:
        if op_id in done:
            continue
        group = next((g for g in merged if op_id == g[0] and set(g) <= set(piece)), None)
        if group:
            sql, key = merged[group]
            out.append((group, sql, key, ()))
            done.update(group)
        else:
            sql, key = op_sql[op_id]
            out.append(((op_id,), sql, key, tuple(prologue.get(op_id, ()))))
    return out


@dataclass(frozen=True)
class MeasurementWindow:
    """One window shared by every accumulator: [start_s, end_s] after the start barrier."""
    start_s: float
    end_s: float

    @property
    def length_s(self):
        return self.end_s - self.start_s

    def contains(self, t_s):
        return self.start_s <= t_s <= self.end_s


@dataclass
class WorkerResult:
    payments: int = 0
    latencies_s: list = field(default_factory=list)
    retries: dict = field(default_factory=dict)     # piece label -> retries, counted payments
    causes: dict = field(default_factory=dict)      # "deadlock" / "serialization" -> count
    holds_s: list = field(default_factory=list)     # warehouse-lock hold time per Payment
    waits_s: list = field(default_factory=list)     # warehouse-lock wait time per Payment
    ttfc_s: list = field(default_factory=list)      # time to the first piece's commit
    tthc_s: list = field(default_factory=list)      # time to the warehouse-lock piece's commit
    timed: int = 0                                  # Payments with a time breakdown
    e2e_timed_s: float = 0.0                        # their end-to-end time, summed
    comp_s: dict = field(default_factory=dict)      # component -> seconds, summed
    stmts: dict = field(default_factory=dict)       # statement key -> [count, seconds]
    abandoned: int = 0


# Time breakdown of a Payment (MODEL.md R22); every interval goes to exactly one component.
COMPONENTS = ("stmt", "commit", "gap_intra", "gap_inter", "retry")


def stmt_key(op_ids, lookup, pre, trigger):
    """Calibration key of a statement: ops, pc's lookup kind, pre-locked or not, trigger."""
    return "|".join(("+".join(op_ids), lookup if "pc" in op_ids else "-",
                     "pre" if pre else "-", trigger))


# Lock-hold timing around a pre-locked statement, in the same call (see module docstring).
# Server timestamps around the pre-lock: requested (before it), granted (after it). HOLD_READ
# returns (wait for the lock, time since it was granted).
HOLD_REQ = "SELECT set_config('cpr.t_req', clock_timestamp()::text, false)"
HOLD_MARK = "SELECT set_config('cpr.t_lock', clock_timestamp()::text, false)"
HOLD_READ = ("SELECT extract(epoch from current_setting('cpr.t_lock')::timestamptz "
             "- current_setting('cpr.t_req')::timestamptz), "
             "extract(epoch from clock_timestamp() - current_setting('cpr.t_lock')::timestamptz)")


def run_payment(conn, op_sql, prologue, pieces, params, deadline, merged=MERGED_SQL,
                timed=False, trigger="", hot_k=None):
    """Execute one Payment piece by piece. Returns (retries per piece, retries per cause,
    (warehouse-lock hold, wait) in s or None, time to first commit, time to the commit of piece
    `hot_k`, time breakdown or None), or None if the deadline passed while a piece was still
    being retried (payment abandoned).

    With `timed`, chained timestamps assign every interval from the start to the last commit to
    one component (COMPONENTS): statement (send -> result), commit, the client's gap before a
    statement (gap_inter before a piece's first statement after the first piece, gap_intra
    otherwise, including the gap before the commit), and retry (a failed attempt, from its start
    to its rollback). So the components add up to the end-to-end time."""
    retries = [0] * len(pieces)
    causes = {}
    vals = dict(params)
    hold_s = None
    statements = [piece_statements(p, op_sql, prologue, merged) for p in pieces]
    clock = time.perf_counter
    t_start = t_mark = clock()
    comp = dict.fromkeys(COMPONENTS, 0.0) if timed else None
    walls = [] if timed else None
    ttfc = tthc = None
    for k, piece in enumerate(pieces):
        while True:
            t_attempt = t_mark
            part = dict.fromkeys(COMPONENTS, 0.0) if timed else None   # this attempt only
            part_walls = []
            try:
                server_s = t_resp = None
                with conn.cursor() as cur:
                    for n, (op_ids, sql, key, pre) in enumerate(statements[k]):
                        if isinstance(sql, dict):       # pc: by id or by last name
                            sql = sql[vals["lookup"]]
                        if pre:
                            # pre-locks go in the same call as their statement: one round trip,
                            # as when the trigger takes the lock inside the statement itself
                            assert key is None, "hold timing needs a statement without result"
                            sql = "; ".join([HOLD_REQ, *pre, HOLD_MARK, sql, HOLD_READ])
                        if timed:
                            now = clock()
                            part["gap_inter" if n == 0 and k > 0 else "gap_intra"] += now - t_mark
                            t_mark = now
                        cur.execute(sql, vals)
                        if pre:
                            wait_s, server_s = map(float, cur.fetchone())
                            t_resp = clock()
                        elif key is not None:
                            # middle row, at position n/2 rounded up (clause 2.5.2.2); a key
                            # lookup returns exactly one row
                            rows = cur.fetchall()
                            assert rows, f"{op_ids} found no row for {vals}"
                            vals[key] = rows[(len(rows) - 1) // 2][0]
                        if timed:
                            now = clock()
                            part["stmt"] += now - t_mark
                            part_walls.append((stmt_key(op_ids, vals["lookup"], pre, trigger),
                                               now - t_mark))
                            t_mark = now
                if timed:
                    now = clock()
                    part["gap_intra"] += now - t_mark
                    t_mark = now
                conn.commit()
                t_commit = clock()
                if timed:
                    part["commit"] += t_commit - t_mark
                    t_mark = t_commit
                    for c in COMPONENTS:
                        comp[c] += part[c]
                    walls += part_walls
                if server_s is not None:
                    hold_s = (server_s + (t_commit - t_resp), wait_s)
                if k == 0:
                    ttfc = t_commit - t_start
                if k == hot_k:
                    tthc = t_commit - t_start
                break
            except RETRYABLE as e:
                conn.rollback()
                if timed:
                    now = clock()
                    comp["retry"] += now - t_attempt
                    t_mark = now
                retries[k] += 1
                cause = ("deadlock" if isinstance(e, psycopg2.errors.DeadlockDetected)
                         else "serialization")
                causes[cause] = causes.get(cause, 0) + 1
                if clock() > deadline:
                    return None
    timing = dict(comp, e2e=t_mark - t_start, walls=walls) if timed else None
    return retries, causes, hold_s, ttfc, tthc, timing


def worker(i, dsn, cc, prologue, pieces, n_wh, window, grace_s, barrier, result, seed,
           op_sql=None, opts=None):
    """One client. opts: merged (R15 statements), timing_every (0 = no breakdown), trigger
    (for the calibration keys), hot_k (index of the piece holding the warehouse lock)."""
    isolation, default_sql, _ = CC[cc]
    op_sql = op_sql or default_sql
    opts = opts or {}
    merged = opts.get("merged", MERGED_SQL)
    every = opts.get("timing_every", 0)
    conn = psycopg2.connect(dsn)
    conn.set_session(isolation_level=isolation, autocommit=False)
    rng = random.Random(seed)
    home_w = i % n_wh + 1
    labels = [piece_label(p) for p in pieces]
    n_run = 0

    barrier.wait()
    t0 = time.perf_counter()    # not monotonic(): that ticks in ~15.6 ms steps on Windows
    while time.perf_counter() - t0 < window.end_s:
        params = payment_params(rng, home_w, n_wh)
        timed = every > 0 and n_run % every == 0
        n_run += 1
        start = time.perf_counter()
        outcome = run_payment(conn, op_sql, prologue, pieces, params,
                              t0 + window.end_s + grace_s, merged=merged, timed=timed,
                              trigger=opts.get("trigger", ""), hot_k=opts.get("hot_k"))
        if outcome is None:
            result.abandoned += 1
            break
        retries, causes, hold_s, ttfc, tthc, timing = outcome
        elapsed = time.perf_counter() - t0
        if window.contains(elapsed):
            result.payments += 1
            result.latencies_s.append(time.perf_counter() - start)
            result.ttfc_s.append(ttfc)
            if tthc is not None:
                result.tthc_s.append(tthc)
            for label, r in zip(labels, retries):
                result.retries[label] = result.retries.get(label, 0) + r
            for cause, n in causes.items():
                result.causes[cause] = result.causes.get(cause, 0) + n
            if hold_s is not None:
                result.holds_s.append(hold_s[0])
                result.waits_s.append(hold_s[1])
            if timing is not None:
                result.timed += 1
                result.e2e_timed_s += timing["e2e"]
                for c in COMPONENTS:
                    result.comp_s[c] = result.comp_s.get(c, 0.0) + timing[c]
                for key, wall in timing["walls"]:
                    s = result.stmts.setdefault(key, [0, 0.0])
                    s[0] += 1
                    s[1] += wall
    conn.close()


def pin_cpus(cpus):
    """Pin this process to the given cores (Linux only); returns what was applied."""
    if not cpus:
        return ""
    if not hasattr(os, "sched_setaffinity"):
        print("  WARNING: --client-cpus ignored (no sched_setaffinity on this OS)")
        return ""
    os.sched_setaffinity(0, cpus)
    return ",".join(map(str, sorted(cpus)))


def client_process(ids, dsn, cc, prologue, pieces, n_wh, window, grace_s, barrier, seed,
                   cpus, queue, op_sql=None, opts=None):
    """One client process: runs its share of the clients as threads, returns their results."""
    pin_cpus(cpus)
    results = [WorkerResult() for _ in ids]
    threads = [threading.Thread(target=worker, args=(i, dsn, cc, prologue, pieces, n_wh, window,
                                                     grace_s, barrier, results[k],
                                                     seed * 1000 + i, op_sql, opts))
               for k, i in enumerate(ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    queue.put(results)


def parse_cpus(spec):
    """'0-7,16' -> {0,...,7,16}; '' -> empty set."""
    out = set()
    for part in filter(None, (spec or "").split(",")):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def server_settings(dsn):
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        out = {}
        for name in ("fsync", "synchronous_commit", "wal_sync_method", "full_page_writes",
                     "server_version", "max_connections", "shared_buffers"):
            cur.execute(f"SHOW {name}")
            out[name] = cur.fetchone()[0]
    conn.close()
    return out


def admin(dsn, *statements):
    """Run statements outside a transaction (VACUUM needs that) and return the last result."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    last = None
    with conn.cursor() as cur:
        for sql in statements:
            cur.execute(sql)
            last = cur.fetchone() if cur.description else None
    conn.close()
    return last


LAYOUTS = ("split", "joined")


def code_version():
    """The commit the code comes from, recorded on every row. In a `git archive` export (the
    server copy) VERSION holds the commit hash; in a working tree git is asked, and "-dirty"
    marks uncommitted changes."""
    try:
        with open(os.path.join(HERE, "VERSION"), encoding="utf-8") as f:
            v = f.read().strip()
        if v and not v.startswith("$Format"):
            return v
    except OSError:
        pass
    try:
        return subprocess.check_output(
            ["git", "-C", HERE, "describe", "--always", "--dirty", "--abbrev=40"],
            text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


CODE_VERSION = code_version()


def setup(dsn, n_wh, layout="split", full_load=False):
    """Load schema and data; layout="joined" puts d_ytd back into district (MODEL.md R14)."""
    assert layout in LAYOUTS
    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
        admin(dsn, f.read())
    admin(dsn, f"SELECT populate({int(n_wh)}, {N_CUST}, {C_LOAD}, {bool(full_load)})")
    if layout == "joined":
        admin(dsn, "SELECT join_district()")
    admin(dsn, "VACUUM ANALYZE")


def district_layout(dsn):
    return "joined" if admin(dsn, "SELECT cpr_table()")[0] == "district" else "split"


# Multixact counters (diagnostic), measured around
# the whole run and divided by all committed Payments (History rows; reset_run empties History).
# pg_control_checkpoint() is only current right after a CHECKPOINT, so one runs before the
# workers start and one after they finish, never during the run. Postgres 16 names the SLRU rows
# MultiXactMember / MultiXactOffset (17: multixact_member / ...), hence the substring match.
MXACT_SQL = ("SELECT next_multixact_id::text::bigint, next_multi_offset::text::bigint "
             "FROM pg_control_checkpoint()")
SLRU_SQL = """
    SELECT count(*) FILTER (WHERE m), count(*) FILTER (WHERE o),
           coalesce(sum(blks_hit) FILTER (WHERE m), 0), coalesce(sum(blks_read) FILTER (WHERE m), 0),
           coalesce(sum(blks_hit) FILTER (WHERE o), 0), coalesce(sum(blks_read) FILTER (WHERE o), 0)
      FROM (SELECT *, replace(lower(name), '_', '') LIKE '%multixactmember%' AS m,
                      replace(lower(name), '_', '') LIKE '%multixactoffset%' AS o
              FROM pg_stat_slru) s"""
MXACT_FIELDS = ["payments_total", "multixacts_created", "multixact_members",
                "mxact_member_hits", "mxact_member_reads", "mxact_offset_hits",
                "mxact_offset_reads"]


def mxact_start(dsn):
    admin(dsn, "CHECKPOINT")
    mx0 = admin(dsn, MXACT_SQL)
    admin(dsn, "SELECT pg_stat_reset_slru(NULL)")
    return mx0


def mxact_end(dsn, mx0):
    admin(dsn, "CHECKPOINT")
    mx1 = admin(dsn, MXACT_SQL)
    n_m, n_o, m_hit, m_read, o_hit, o_read = admin(dsn, SLRU_SQL)
    if n_m != 1 or n_o != 1:
        raise RuntimeError(f"pg_stat_slru: expected one MultiXactMember and one MultiXactOffset "
                           f"row, found {n_m} and {n_o}")
    return {"payments_total": admin(dsn, "SELECT count(*) FROM history")[0],
            "multixacts_created": mx1[0] - mx0[0], "multixact_members": mx1[1] - mx0[1],
            "mxact_member_hits": m_hit, "mxact_member_reads": m_read,
            "mxact_offset_hits": o_hit, "mxact_offset_reads": o_read}


# Wait sampling (MODEL.md R18). A row-lock wait shows only the holder's transaction id, not the
# row, so the waiting statement identifies the lock; advisory locks are mix.py's table locks.
WAIT_SQL = """
    SELECT state, wait_event_type, wait_event, query FROM pg_stat_activity
     WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()"""
WAIT_FIELDS = ["wait_payment_cpr", "wait_neworder_district", "wait_district_tables",
               "wait_customer", "wait_stock", "wait_other_lock", "in_txn_client", "in_server"]


def wait_category(state, wtype, event, query):
    if wtype == "Lock":
        if event == "advisory":
            return "wait_district_tables"          # mix.py: NewOrder, Delivery, OrderStatus
        if "FOR NO KEY UPDATE" in query and "FROM warehouse" in query:
            return "wait_payment_cpr"              # warehouse pre-lock + district update (+CPR)
        if "d_next_o_id = d_next_o_id + 1" in query:
            return "wait_neworder_district"        # NewOrder's district row
        if "customer" in query:
            return "wait_customer"
        if "stock" in query:
            return "wait_stock"
        return "wait_other_lock"
    if state == "idle in transaction":
        return "in_txn_client"            # holds its locks while the client works
    if state == "active":
        return "in_server"                # running, or waiting on something other than a lock
    return None                           # between transactions


def cpu_times(cpus):
    """{cpu: (busy, total)} jiffies of the given cores from /proc/stat (Linux; the container
    shares the host's cores), or None where that is not available."""
    if not cpus:
        return None
    try:
        out = {}
        with open("/proc/stat", encoding="ascii") as f:
            for line in f:
                if line.startswith("cpu") and line[3].isdigit():
                    parts = line.split()
                    if int(parts[0][3:]) in cpus:
                        v = [int(x) for x in parts[1:9]]     # user .. steal, without guest
                        out[int(parts[0][3:])] = (sum(v) - v[3] - v[4], sum(v))  # minus idle, iowait
        return out
    except OSError:
        return None


def cpu_util(t0, t1):
    """(mean, max) busy share over the cores between two cpu_times readings."""
    if not (t0 and t1):
        return float("nan"), float("nan")
    shares = [(t1[c][0] - t0[c][0]) / (t1[c][1] - t0[c][1])
              for c in t0 if c in t1 and t1[c][1] > t0[c][1]]
    if not shares:
        return float("nan"), float("nan")
    return sum(shares) / len(shares), max(shares)


class WaitSampler(threading.Thread):
    """Samples where the clients wait, from `start_s` to `end_s` after all clients have
    connected (the workers' start barrier follows right after). Also reads the busy share of
    `pg_cpus` (Postgres's cores) and `client_cpus` over the same window."""
    def __init__(self, dsn, clients, start_s, end_s, interval_s=0.1, pg_cpus=frozenset(),
                 client_cpus=frozenset()):
        super().__init__(daemon=True)
        self.dsn, self.clients, self.start_s, self.end_s = dsn, clients, start_s, end_s
        self.interval_s, self.counts, self.samples = interval_s, {}, 0
        self.pg_cpus, self.cpu0, self.cpu1 = pg_cpus, None, None
        self.client_cpus, self.ccpu0, self.ccpu1 = client_cpus, None, None

    def run(self):
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            for _ in range(600):                   # up to 60 s for client processes to start
                cur.execute("SELECT count(*) FROM pg_stat_activity WHERE backend_type = "
                            "'client backend' AND pid <> pg_backend_pid()")
                if cur.fetchone()[0] >= self.clients:
                    break
                time.sleep(0.1)
            t0 = time.perf_counter()
            time.sleep(self.start_s)
            self.cpu0 = cpu_times(self.pg_cpus)
            self.ccpu0 = cpu_times(self.client_cpus)
            while time.perf_counter() - t0 < self.end_s:
                cur.execute(WAIT_SQL)
                for row in cur.fetchall():
                    cat = wait_category(*row)
                    if cat:
                        self.counts[cat] = self.counts.get(cat, 0) + 1
                self.samples += 1
                time.sleep(self.interval_s)
            self.cpu1 = cpu_times(self.pg_cpus)
            self.ccpu1 = cpu_times(self.client_cpus)
        conn.close()

    def fractions(self):
        """{field: average fraction of the clients in that state}."""
        n = self.samples * self.clients
        return {f: self.counts.get(f, 0) / n if n else float("nan") for f in WAIT_FIELDS}

    def lock_waiting(self):
        """Average number of clients waiting for a lock (any wait_* category)."""
        if not self.samples:
            return float("nan")
        return sum(v for k, v in self.counts.items() if k.startswith("wait_")) / self.samples

    def cpu_util(self):
        """Mean busy share of Postgres's cores."""
        return cpu_util(self.cpu0, self.cpu1)[0]

    def client_cpu_util(self):
        """(mean, busiest core) busy share of the client cores. The busiest core near 1 means
        a client process is CPU-bound (one Python process runs on one core at a time)."""
        return cpu_util(self.ccpu0, self.ccpu1)


# Lock waits logged by Postgres (log_lock_waits = on, deadlock_timeout = 1 ms; service
# pg-tmpfs-locklog). Every wait longer than deadlock_timeout logs "process P acquired MODE on
# OBJECT after X ms" when it ends. Read from the server's log file (logging_collector) over the
# whole run, so a run reports the waits of at least 1 ms per committed Payment.
LOCKLOG_RE = re.compile(r"acquired \S+ on .+? after (\d+(?:\.\d+)?) ms")


def locklog_start(dsn):
    """None if lock waits are not logged, else (log file, its size now)."""
    if admin(dsn, "SHOW log_lock_waits")[0] != "on":
        return None
    path = admin(dsn, "SELECT pg_current_logfile()")[0]
    if not path:
        raise RuntimeError("log_lock_waits is on but logging_collector is off")
    return path, admin(dsn, f"SELECT size FROM pg_stat_file('{path}')")[0]


def locklog_end(dsn, mark):
    """(logged waits, their total in ms) since locklog_start, or NaNs."""
    if mark is None:
        return float("nan"), float("nan")
    path, size0 = mark
    size1 = admin(dsn, f"SELECT size FROM pg_stat_file('{path}')")[0]
    if admin(dsn, "SELECT pg_current_logfile()")[0] != path or size1 < size0:
        raise RuntimeError("the server log was rotated during the run (set log_rotation_size=0)")
    text = admin(dsn, f"SELECT pg_read_file('{path}', {size0}, {size1 - size0})")[0]
    waits = [float(x) for x in LOCKLOG_RE.findall(text)]
    return len(waits), sum(waits)


def load_calibration(path):
    """Statement baselines written by calibrate.py: {"hi": {key: s}, "lo": {clients: {key: s}}}."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def lo_baseline(cal, clients):
    """The load-matched baseline for `clients`: the smallest calibrated client count at or above
    it (more load, so a larger baseline and a lower lock-wait estimate), else the largest."""
    lo = {int(c): b for c, b in (cal or {}).get("lo", {}).items()}
    if not lo:
        return None, ""
    c = min((c for c in lo if c >= clients), default=max(lo))
    return lo[c], c


def lock_wait_ms(stmts, base, timed):
    """Mean lock wait per timed Payment: statement time above the baseline, summed over the
    statements (a difference of means, so it is unbiased for the mean; per statement it can be
    negative). NaN without a baseline for every statement key that ran."""
    if not base or not timed or any(k not in base for k in stmts):
        return float("nan")
    return sum(s - n * base[k] for k, (n, s) in stmts.items()) / timed * 1000.0


TIMING_FIELDS = (
    [f"{m}_{q}_ms" for m in ("ttfc", "tthc") for q in ("mean", "p50", "p95", "p99")]
    + ["timing_every", "timed_payments", "t_e2e_ms"] + [f"t_{c}_ms" for c in COMPONENTS]
    + ["t_lockwait_hi_ms", "t_lockwait_lo_ms", "lo_baseline_clients", "sampler_lockwait_ms",
       "pg_cpu_util", "client_cpu_util", "client_cpu_max", "locklog_waits_per_payment", "locklog_wait_ms_per_payment",
       "merge_same_row"])


def run_cell(dsn, arm, n_wh, clients, duration_s=30.0, warmup_s=2.0, grace_s=10.0, seed=0,
             cc="2pl", procs=1, client_cpus="", timing_every=1, calibration=None, pg_cpus=""):
    """One measurement point. Expects populate() for n_wh to have run. procs > 1 spreads the
    clients over that many processes; client_cpus pins the client processes (Linux).
    timing_every: time breakdown for every Nth Payment of a client (0 = none; "@notiming" arms
    always 0). calibration: baselines from load_calibration (lock-wait columns NaN without).
    pg_cpus: Postgres's cores, for pg_cpu_util (Linux). The returned row carries the summed
    statement times per calibration key under "_stmts" (sweep.py writes them to a sidecar)."""
    variant, pieces = pieces_for(arm, cc)
    prologue = prologue_for(arm, cc)
    op_sql = CC[cc][1]
    if arm_variant(arm) == "@keyshare":
        assert cc == "2pl", "@keyshare is a 2PL variant"
        op_sql = OP_SQL_KEYSHARE                           # protocol before Oct 14
    if arm_variant(arm) == "@notiming":
        timing_every = 0
    merged = merged_for(arm)
    hot_k = next(k for k, p in enumerate(pieces) if "pa3" in p)   # holds the warehouse lock
    opts = {"merged": merged, "timing_every": timing_every, "trigger": variant, "hot_k": hot_k}
    admin(dsn, "SELECT reset_run()", f"SELECT set_cpr('{variant}')",
          "VACUUM ANALYZE warehouse, district, customer, history")
    cnt0, bal0, ytd0 = admin(dsn, "SELECT * FROM customer_totals()")
    window = MeasurementWindow(warmup_s, warmup_s + duration_s)
    mx0 = mxact_start(dsn)
    lock_log = locklog_start(dsn)
    sampler = WaitSampler(dsn, clients, warmup_s, warmup_s + duration_s,
                          pg_cpus=frozenset(parse_cpus(pg_cpus)),
                          client_cpus=frozenset(parse_cpus(client_cpus)))
    sampler.start()
    cpus = parse_cpus(client_cpus)
    procs = max(1, min(procs, clients))
    if procs == 1:
        applied = pin_cpus(cpus)
        barrier = threading.Barrier(clients)
        results = [WorkerResult() for _ in range(clients)]
        threads = [threading.Thread(target=worker, args=(i, dsn, cc, prologue, pieces, n_wh,
                                                         window, grace_s, barrier, results[i],
                                                         seed * 1000 + i, op_sql, opts))
                   for i in range(clients)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        ctx = mp.get_context("spawn")
        barrier, queue = ctx.Barrier(clients), ctx.Queue()
        shares = [list(range(clients))[k::procs] for k in range(procs)]
        ps = [ctx.Process(target=client_process,
                          args=(ids, dsn, cc, prologue, pieces, n_wh, window, grace_s, barrier,
                                seed, cpus, queue, op_sql, opts))
              for ids in shares]
        for pr in ps:
            pr.start()
        results = [r for _ in ps for r in queue.get()]
        for pr in ps:
            pr.join()
        applied = ",".join(map(str, sorted(cpus)))

    sampler.join()
    n_logged, logged_ms = locklog_end(dsn, lock_log)
    mxact = mxact_end(dsn, mx0)
    payments = sum(r.payments for r in results)
    lat = sorted(x for r in results for x in r.latencies_s)
    retries = {}
    for r in results:
        for label, n in r.retries.items():
            retries[label] = retries.get(label, 0) + n
    total_retries = sum(retries.values())
    causes = {}
    for r in results:
        for cause, n in r.causes.items():
            causes[cause] = causes.get(cause, 0) + n
    wytd = next(piece_label(p) for p in pieces if "pa3" in p)   # the piece writing w_ytd
    abandoned = sum(r.abandoned for r in results)
    cc1, cc8, cc9, cust = admin(dsn, f"SELECT * FROM consistency_violations("
                                     f"{cnt0}, {bal0}, {ytd0})")
    violations = cc1 + cc8 + cc9 + cust
    holds = sorted(x for r in results for x in r.holds_s)
    waits = sorted(x for r in results for x in r.waits_s)
    quantile = lambda xs, f: xs[min(int(f * len(xs)), len(xs) - 1)] * 1000.0 if xs else float("nan")
    q = lambda f: quantile(lat, f)
    nan = float("nan")
    timing = {}
    for name, xs in (("ttfc", sorted(x for r in results for x in r.ttfc_s)),
                     ("tthc", sorted(x for r in results for x in r.tthc_s))):
        timing[f"{name}_mean_ms"] = sum(xs) / len(xs) * 1000.0 if xs else nan
        for f in (0.50, 0.95, 0.99):
            timing[f"{name}_p{round(f * 100)}_ms"] = quantile(xs, f)
    timed = sum(r.timed for r in results)
    stmts = {}
    for r in results:
        for key, (n, s) in r.stmts.items():
            acc = stmts.setdefault(key, [0, 0.0])
            acc[0] += n
            acc[1] += s
    per_timed = lambda s: s / timed * 1000.0 if timed else nan
    timing.update(timing_every=timing_every, timed_payments=timed,
                  t_e2e_ms=per_timed(sum(r.e2e_timed_s for r in results)))
    for c in COMPONENTS:
        timing[f"t_{c}_ms"] = per_timed(sum(r.comp_s.get(c, 0.0) for r in results))
    lo, lo_clients = lo_baseline(calibration, clients)
    timing.update(t_lockwait_hi_ms=lock_wait_ms(stmts, (calibration or {}).get("hi"), timed),
                  t_lockwait_lo_ms=lock_wait_ms(stmts, lo, timed),
                  lo_baseline_clients=lo_clients,
                  sampler_lockwait_ms=(sampler.lock_waiting() * window.length_s / payments
                                       * 1000.0 if payments else nan),
                  pg_cpu_util=sampler.cpu_util(),
                  client_cpu_util=sampler.client_cpu_util()[0],
                  client_cpu_max=sampler.client_cpu_util()[1],
                  locklog_waits_per_payment=(n_logged / mxact["payments_total"]
                                             if mxact["payments_total"] else nan),
                  locklog_wait_ms_per_payment=(logged_ms / mxact["payments_total"]
                                               if mxact["payments_total"] else nan),
                  merge_same_row=bool(merged))
    return {
        "code_version": CODE_VERSION,
        "cc": cc, "arm": arm, "warehouses": n_wh, "clients": clients, "seed": seed,
        "client_procs": procs, "client_cpus": applied, "host": socket.gethostname(),
        "pieces": len(pieces), "district_layout": district_layout(dsn), **mxact,
        **sampler.fractions(),
        "throughput_pps": payments / window.length_s,
        "payments": payments,
        "retries_per_payment": total_retries / payments if payments else float("nan"),
        "wytd_piece": wytd,
        "wytd_retries_per_payment": retries.get(wytd, 0) / payments if payments else float("nan"),
        "retries_by_piece": ";".join(f"{k}={v}" for k, v in sorted(retries.items())),
        "deadlocks": causes.get("deadlock", 0),
        "serialization_failures": causes.get("serialization", 0),
        "latency_mean_ms": sum(lat) / len(lat) * 1000.0 if lat else float("nan"),
        "latency_p50_ms": q(0.50), "latency_p95_ms": q(0.95), "latency_p99_ms": q(0.99),
        "wlock_hold_p50_ms": quantile(holds, 0.50), "wlock_hold_p95_ms": quantile(holds, 0.95),
        "wlock_wait_p50_ms": quantile(waits, 0.50), "wlock_wait_p95_ms": quantile(waits, 0.95),
        "wlock_wait_mean_ms": sum(waits) / len(waits) * 1000.0 if waits else nan,
        **timing,
        "abandoned": abandoned,
        "cc1_violations": cc1, "cc8_violations": cc8, "cc9_violations": cc9,
        "customer_violations": cust,
        "status": "OK" if payments and not abandoned and violations == 0 else "FLAG",
        "_stmts": stmts,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=DSN_TMPFS)
    ap.add_argument("--plan", action="store_true", help="print each arm's pieces and exit")
    ap.add_argument("--setup", action="store_true", help="load schema and data, then exit")
    ap.add_argument("--arm", help="one of " + ", ".join(ARMS) + ", or a coarsening arm "
                    "such as B-inc/k3 (see --plan --coarsen)")
    ap.add_argument("--coarsen", action="store_true", help="with --plan: print the chain")
    ap.add_argument("--contiguous", action="store_true",
                    help="with --plan: print every contiguous arm (E3) and what it issues")
    ap.add_argument("--procs", type=int, default=1, help="client processes (default 1)")
    ap.add_argument("--client-cpus", default="", help="pin clients to cores, e.g. 32-47 (Linux)")
    ap.add_argument("--pg-cpus", default="", help="Postgres's cores, for pg_cpu_util (Linux)")
    ap.add_argument("--timing-every", type=int, default=1,
                    help="time breakdown for every Nth Payment per client (0 = none)")
    ap.add_argument("--calibration", default="", help="baseline JSON from calibrate.py")
    ap.add_argument("--cc", choices=sorted(CC), default="2pl",
                    help="2pl = strict 2PL on rows (default); ssi = SERIALIZABLE")
    ap.add_argument("--warehouses", type=int, default=1)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--duration-s", type=float, default=30.0)
    ap.add_argument("--warmup-s", type=float, default=2.0)
    args = ap.parse_args()

    if args.plan and args.arm:
        if not is_arm(args.arm):
            ap.error(f"unknown arm {args.arm}")
        print_issued(args.arm, args.cc)
        return
    if args.plan and args.contiguous:
        for scenario in SCENARIOS:
            k = len(finest_pieces(scenario))
            print(f"{scenario}: finest chopping, emitted order: "
                  + "  ".join("{" + piece_label(p) + "}" for p in finest_pieces(scenario)))
            arms = contiguous_arms(scenario)
            for j in range(k - 1, 1, -1):
                of_j = [a for a in arms if len(a.split("/b")[1].split(".")) == j]
                print(f"  j = {j}: {len(of_j)} arms (C({k - 1},{j - 1}) = {math.comb(k - 1, j - 1)})")
                for a in of_j:
                    print_issued(a, args.cc, indent="    ")
        e3 = e3_arms()
        print(f"E3 arm set: {len(e3)} arms (per trigger: unchopped, chopped, contiguous); "
              "all validated")
        return
    if args.plan and args.coarsen:
        for scenario in SCENARIOS:
            for pieces, kinds in coarsening_chain(scenario):
                print(f"{scenario}/k{len(pieces)}  " + "  ".join(
                    "{" + piece_label(p) + "}" + k[0] for p, k in zip(pieces, kinds)))
        print("piece kinds: c = contended, i = free increment (hot lock), r = conflict-free read")
        for arm in EXPLICIT_ARMS:
            _, pieces = pieces_for(arm)
            print(f"{arm:24s} " + "  ".join("{" + piece_label(p) + "}" for p in pieces)
                  + "   (explicit, checked)")
        return
    if args.plan:
        for arm in ARMS:
            variant, pieces = pieces_for(arm, args.cc)
            locks = ",".join(f"{k}:{len(v)}" for k, v in prologue_for(arm, args.cc).items())
            print(f"{arm:16s} trigger={variant} prelock={locks or '-'}  "
                  + "  ".join("{" + piece_label(p) + "}"
                                                               for p in pieces))
        return
    if args.setup:
        setup(args.dsn, args.warehouses)
        print(f"loaded {args.warehouses} warehouse(s); settings: {server_settings(args.dsn)}")
        return
    if not args.arm or not is_arm(args.arm):
        ap.error("--arm is required unless --plan or --setup (see --help)")
    row = run_cell(args.dsn, args.arm, args.warehouses, args.clients,
                   args.duration_s, args.warmup_s, cc=args.cc, procs=args.procs,
                   client_cpus=args.client_cpus, timing_every=args.timing_every,
                   calibration=load_calibration(args.calibration), pg_cpus=args.pg_cpus)
    for k, v in row.items():
        if not k.startswith("_"):
            print(f"{k:26s} {v}")


def print_issued(arm, cc="2pl", indent=""):
    """One line per arm: its pieces, then the statements as the runner issues them
    (pre-locks marked with !, ops merged into one statement joined with &)."""
    _, pieces = pieces_for(arm, cc)
    pro = prologue_for(arm, cc)
    issued = " | ".join(" ".join(("!" if pre else "") + "&".join(ids)
                                 for ids, _, _, pre in piece_statements(p, OP_SQL, pro,
                                                                        merged_for(arm)))
                        for p in pieces)
    print(f"{indent}{arm:24s} " + " ".join("{" + piece_label(p) + "}" for p in pieces)
          + f"   issues: {issued}")


if __name__ == "__main__":
    main()

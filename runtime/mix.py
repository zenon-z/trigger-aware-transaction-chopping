"""
Runtime check, full TPC-C mix (not used in the paper): the same 2x2 as the
Payment-only check (payment.py), B-inc / B-rec x chopped / unchopped, with all five transaction
types at the spec's mix. Shows whether Payment's chopping effect survives in a realistic workload.

- Pieces come from chop.py for the full mix: every template is chopped as FineChop chops it
  (`--plan` prints them); unchopped = each template as one piece. Payment's pieces are the same
  as in the Payment-only workload, and Payment runs through the same statements, inputs, pre-lock
  rule and hold timing (payment.py). Stock-Level runs below serializability, as the spec allows
  (MODEL.md S7): one READ COMMITTED transaction without locks, never chopped.
- Strict 2PL on rows, as in payment.py (MODEL.md F6), plus district-level locks for the row sets
  that inserts and deletes change (MODEL.md R13): Order, NewOrder and OrderLine rows of one
  district. A piece whose ops insert or delete such rows takes the district's lock for that table
  exclusively; a piece whose ops search them takes it shared. Taken at the start of the piece, in
  a fixed table order, in the same call as the piece's first statement (Postgres advisory
  transaction locks, held to commit). They stand in for the predicate locks Postgres does not
  offer under 2PL: every such search in TPC-C stays within one district.
- Ops the spec performs as one statement on one row run as that statement when they share a
  piece (MODEL.md R15): NewOrder's stock update of an item (no8-no10), Delivery's customer update
  (de6, de7), Payment's customer update (pa5, pa6).
- NewOrder: chop.py unrolls the item loop to two iterations; at runtime items 1..n-1 run the ops
  of item `a`, the last item those of item `b` (the one that can be unused, which rolls the
  transaction back in its first piece). Items 1..n-1 are processed in (supply warehouse, item
  id) order to avoid deadlocks between NewOrders; the last item stays last (MODEL.md R16).
- Delivery: one database transaction per district (clause 2.7.4.1, MODEL.md S6), each chopped,
  run by the client itself instead of a deferred queue. A Delivery counts when all 10 districts
  are done; a district without an undelivered order is skipped (clause 2.7.4.2).
- Every row records the district layout (MODEL.md R14) and where the clients wait (R18, the
  sampler in payment.py): this shows where the bottleneck sits, e.g. the warehouse row
  (Payment) or the district table locks (NewOrder per district).
- The database is not reset between runs. After each run, mix.py checks TPC-C consistency
  conditions 1-10 and 12 on the whole database (schema.sql consistency_mix); any violation flags
  the run. `check="light"` checks only conditions 1, 2, 3, 8 and 9 per run (the rest scan every
  order line and customer, about 20 s at 32 warehouses); sweep.py then runs the full check at
  the end of each warehouse block (full_check) and stops on a violation.

Usage:
  python mix.py --plan                          # pieces and table locks of each template per arm
  python mix.py --setup --warehouses 2          # load the full TPC-C database
  python mix.py --arm B-inc/chopped --warehouses 2 --clients 8
"""
import argparse
import multiprocessing as mp
import os
import random
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import payment as P  # noqa: E402
from payment import chop  # noqa: E402

ARMS = P.ARMS
TYPES = ["NewOrder", "Payment", "OrderStatus", "Delivery", "StockLevel"]
MIX_WEIGHTS = [45, 43, 4, 4, 4]            # clause 5.2.3: minimum mix, NewOrder the rest
N_ITEMS = 100_000
C_OL_I_ID = 4011                           # NURand constant for item ids (clause 2.1.6)
# District-level table locks for row sets that inserts and deletes change (MODEL.md R13):
# chop.py cell -> (advisory lock class, table). Acquired in this order.
TABLE_LOCKS = {"o_row": (1, "orders"), "no_row": (2, "new_order"), "ol_row": (3, "order_line")}

OP_SQL = dict(P.OP_SQL_2PL, **{
    # NewOrder ops that are not per item (the per-item ones are built in neworder_statements)
    # static reads (w_tax, d_tax, c_discount, item) take no lock (MODEL.md F6)
    "no1": ("SELECT w_tax FROM warehouse WHERE w_id = %(w)s", "w_tax"),
    "no2": ("SELECT d_tax FROM district WHERE d_w_id = %(w)s AND d_id = %(d)s", "d_tax"),
    "no3": ("UPDATE district SET d_next_o_id = d_next_o_id + 1 "
            "WHERE d_w_id = %(w)s AND d_id = %(d)s RETURNING d_next_o_id - 1", "o_id"),
    "no4": ("SELECT c_discount, c_last, c_credit FROM customer "
            "WHERE c_w_id = %(w)s AND c_d_id = %(d)s AND c_id = %(c)s",
            "c_discount"),
    "no5": ("INSERT INTO orders VALUES (%(w)s, %(d)s, %(o_id)s, %(c)s, now(), NULL, %(ol_cnt)s, "
            "%(all_local)s)", None),
    "no6": ("INSERT INTO new_order VALUES (%(w)s, %(d)s, %(o_id)s)", None),
    # OrderStatus: customer by id or last name (same statements as Payment's pc), then the
    # balance, the most recent order and its lines. Dynamic cells take FOR SHARE.
    "osc": P.OP_SQL_2PL["pc"],
    "os1": ("SELECT c_balance, c_first, c_middle, c_last FROM customer "
            "WHERE c_w_id = %(w)s AND c_d_id = %(d)s AND c_id = %(c_id)s FOR SHARE", "c_balance"),
    "os2": ("SELECT o_id, o_entry_d, o_carrier_id FROM orders "
            "WHERE o_w_id = %(w)s AND o_d_id = %(d)s AND o_c_id = %(c_id)s "
            "ORDER BY o_id DESC LIMIT 1 FOR SHARE", None),
    "os3": ("SELECT ol_i_id, ol_supply_w_id, ol_quantity, ol_amount, ol_delivery_d "
            "FROM order_line WHERE ol_w_id = %(w)s AND ol_d_id = %(d)s AND ol_o_id = %(o_id)s "
            "FOR SHARE", None),
    # Delivery, one district
    "de1": ("SELECT no_o_id FROM new_order WHERE no_w_id = %(w)s AND no_d_id = %(d)s "
            "ORDER BY no_o_id LIMIT 1 FOR UPDATE", None),
    "de2": ("DELETE FROM new_order WHERE no_w_id = %(w)s AND no_d_id = %(d)s "
            "AND no_o_id = %(o_id)s", None),
    "de3": ("UPDATE orders SET o_carrier_id = %(carrier)s "
            "WHERE o_w_id = %(w)s AND o_d_id = %(d)s AND o_id = %(o_id)s RETURNING o_c_id", "c_id"),
    "de4": ("UPDATE order_line SET ol_delivery_d = now() "
            "WHERE ol_w_id = %(w)s AND ol_d_id = %(d)s AND ol_o_id = %(o_id)s", None),
    # ol_amount is never written after insert; the order's lines are covered by the order_line
    # table lock (R13), so the sum needs no row lock (F6)
    "de5": ("SELECT sum(ol_amount) FROM order_line "
            "WHERE ol_w_id = %(w)s AND ol_d_id = %(d)s AND ol_o_id = %(o_id)s", "ol_total"),
    "de6": ("UPDATE customer SET c_balance = c_balance + %(ol_total)s "
            "WHERE c_w_id = %(w)s AND c_d_id = %(d)s AND c_id = %(c_id)s", None),
    "de7": ("UPDATE customer SET c_delivery_cnt = c_delivery_cnt + 1 "
            "WHERE c_w_id = %(w)s AND c_d_id = %(d)s AND c_id = %(c_id)s", None),
})
MERGED_SQL = {**P.MERGED_SQL,
    ("de6", "de7"): ("UPDATE customer SET c_balance = c_balance + %(ol_total)s, "
                     "c_delivery_cnt = c_delivery_cnt + 1 "
                     "WHERE c_w_id = %(w)s AND c_d_id = %(d)s AND c_id = %(c_id)s", None),
}


class UserRollback(Exception):
    """NewOrder's unused item: the transaction rolls back (clause 2.4.2.3)."""


class Skip(Exception):
    """Delivery: the district has no undelivered order (clause 2.7.4.2)."""


def de1_result(rows, vals):
    if not rows:
        raise Skip()
    vals["o_id"] = rows[0][0]


def os2_result(rows, vals):
    vals["o_id"] = rows[0][0] if rows else None


HANDLERS = {"de1": de1_result, "os2": os2_result}


@dataclass
class Stmt:
    sql: object                                   # str, or {lookup: str} for the customer read
    key: Optional[str] = None                     # store the (middle) row's first column
    handler: Optional[Callable] = None            # handler(rows, vals) instead of key
    pre: tuple = ()                               # pre-lock statements (hold timing)
    params: Optional[Callable] = None             # params(vals) -> extra parameters


# ------------------------------------------------------------------------------- plan
def templates_for(scenario):
    sc = chop.TPCC_SCENARIOS[scenario]
    return {**chop.TPCC, **sc.get("templates", {})}, sc["cprs"]


def plan(arm):
    """{template: [(op ids, table locks)]} in execution order. Table locks: [(class, mode)]."""
    scenario, mode = arm.split("/")
    tpls, cprs = templates_for(scenario)
    if mode == "unchopped":
        pieces = {n: [[o["id"] for o in t["ops"]]] for n, t in tpls.items() if not t["weak"]}
    elif mode == "chopped":
        pieces = {n: [ids for ids, _, _ in r[0]] for n, r in chop.chop_config(tpls, cprs).items()}
    else:
        raise ValueError(f"mix supports {ARMS}, not {arm}")
    out = {}
    for name, ps in pieces.items():
        byid = {o["id"]: o for o in tpls[name]["ops"]}
        out[name] = [(p, table_locks([byid[x] for x in p])) for p in ps]
    return out


def table_locks(ops):
    """District table locks a piece needs: exclusive if one of its ops writes the row set."""
    modes = {}
    for o in ops:
        for cell in o["attrs"] & TABLE_LOCKS.keys():
            cls = TABLE_LOCKS[cell][0]
            modes[cls] = "X" if chop.writes(o) or modes.get(cls) == "X" else "S"
    return sorted(modes.items())


def lock_sql(locks):
    return [f"SELECT pg_advisory_xact_lock{'' if m == 'X' else '_shared'}({cls}, %(gk)s)"
            for cls, m in locks]


# ------------------------------------------------------------------------------- statements
def neworder_statements(piece, vals):
    """NewOrder's statements for a piece: header ops in order; the item ops of group `a` for
    items 1..n-1 and of group `b` for item n, one item after another."""
    out, groups_done = [], set()
    items = vals["items"]
    for op_id in piece:
        m = re.fullmatch(r"no(\d+)([ab])", op_id)
        if not m:
            sql, key = OP_SQL[op_id]
            out.append(Stmt(sql, key))
            continue
        g = m.group(2)
        if g in groups_done:
            continue
        groups_done.add(g)
        nums = {int(re.fullmatch(r"no(\d+)" + g, x).group(1)) for x in piece
                if re.fullmatch(r"no\d+" + g, x)}
        for k in (range(len(items) - 1) if g == "a" else [len(items) - 1]):
            out += item_statements(k, nums, vals["d"])
    return out


def item_statements(k, nums, d):
    """Item k: read (no7), one stock update with the ops of no8-no10 present (R15), order line
    insert (no11)."""
    def item_params(vals):
        i, sw, q = vals["items"][k]
        return {"i": i, "sw": sw, "q": q, "remote": int(sw != vals["w"]), "k": k + 1}

    def item_result(rows, vals):
        if not rows:
            raise UserRollback()
        vals[f"price{k}"] = rows[0][0]

    def stock_result(rows, vals):
        vals[f"dist{k}"] = rows[0][1]

    def line_params(vals):
        p = item_params(vals)
        p.update(amount=round(p["q"] * float(vals[f"price{k}"]), 2), dist=vals[f"dist{k}"])
        return p

    out = []
    if 7 in nums:
        out.append(Stmt("SELECT i_price, i_name, i_data FROM item WHERE i_id = %(i)s",
                        handler=item_result, params=item_params))
    sets = []
    if 8 in nums:
        sets.append("s_quantity = CASE WHEN s_quantity >= %(q)s + 10 THEN s_quantity - %(q)s "
                    "ELSE s_quantity - %(q)s + 91 END")
    if 9 in nums:
        sets.append("s_ytd = s_ytd + %(q)s")
    if 10 in nums:
        sets.append("s_order_cnt = s_order_cnt + 1, s_remote_cnt = s_remote_cnt + %(remote)s")
    if sets:
        sql = ("UPDATE stock SET " + ", ".join(sets) + " WHERE s_w_id = %(sw)s AND s_i_id = %(i)s")
        if 8 in nums:
            out.append(Stmt(sql + f" RETURNING s_quantity, s_dist_{d:02d}, s_data",
                            handler=stock_result, params=item_params))
        else:
            out.append(Stmt(sql, params=item_params))
    if 11 in nums:
        out.append(Stmt("INSERT INTO order_line VALUES (%(w)s, %(d)s, %(o_id)s, %(k)s, %(i)s, "
                        "%(sw)s, NULL, %(q)s, %(amount)s, %(dist)s)", params=line_params))
    return out


def piece_stmts(tname, piece, vals, prologue):
    if tname == "NewOrder":
        return neworder_statements(piece, vals)
    pre = prologue if tname == "Payment" else {}
    return [Stmt(sql, key, HANDLERS.get(ops[0]), pre_)
            for ops, sql, key, pre_ in P.piece_statements(piece, OP_SQL, pre, MERGED_SQL)]


# ------------------------------------------------------------------------------- inputs
def neworder_params(rng, w, n_wh):
    """Clause 2.4.1: district, customer, 5-15 items (1% remote supply), 1% unused last item."""
    d, c = rng.randint(1, 10), P.nurand(rng, 1023, 1, P.N_CUST, P.C_ID)
    n, rbk = rng.randint(5, 15), rng.randint(1, 100)
    items = []
    for k in range(n):
        i = N_ITEMS + 1 if (k == n - 1 and rbk == 1) else P.nurand(rng, 8191, 1, N_ITEMS, C_OL_I_ID)
        sw = w
        if n_wh > 1 and rng.randint(1, 100) == 1:
            sw = rng.choice([x for x in range(1, n_wh + 1) if x != w])
        items.append((i, sw, rng.randint(1, 10)))
    items = sorted(items[:-1], key=lambda t: (t[1], t[0])) + items[-1:]   # R16
    return {"w": w, "d": d, "c": c, "items": items, "ol_cnt": n,
            "all_local": int(all(sw == w for _, sw, _ in items))}


def orderstatus_params(rng, w):
    """Clause 2.6.1: home warehouse, random district, 60% by last name."""
    p = P.payment_params(rng, w, 1)          # same customer selection as Payment, never remote
    return {"w": w, "d": p["cd"], "cw": w, "cd": p["cd"], "lookup": p["lookup"],
            "c_last": p["c_last"], "c": p["c"]}


# ------------------------------------------------------------------------------- execution
def run_pieces(conn, tname, pieces, vals, deadline, prologue):
    """Execute one transaction piece by piece (one database transaction per piece, retried on
    deadlock/serialization failure). Returns (outcome, retries, causes, hold) with outcome
    'commit', 'rollback' or 'skip' and hold = (warehouse-lock hold, wait) in s for Payment's CPR
    piece (else None), or None if the deadline passed during retries."""
    retries, causes, hold_s = 0, {}, None
    vals["gk"] = vals["w"] * 100 + vals["d"]
    for piece, locks in pieces:
        while True:
            try:
                server_s = t_resp = None
                with conn.cursor() as cur:
                    for j, st in enumerate(piece_stmts(tname, piece, vals, prologue)):
                        sql = st.sql[vals["lookup"]] if isinstance(st.sql, dict) else st.sql
                        params = dict(vals, **st.params(vals)) if st.params else vals
                        if j == 0 and locks:
                            assert not st.pre
                            sql = "; ".join(lock_sql(locks) + [sql])
                        if st.pre:
                            assert st.key is None and st.handler is None
                            sql = "; ".join([P.HOLD_REQ, *st.pre, P.HOLD_MARK, sql,
                                             P.HOLD_READ])
                        cur.execute(sql, params)
                        if st.pre:
                            wait_s, server_s = map(float, cur.fetchone())
                            t_resp = time.perf_counter()
                        elif st.handler:
                            st.handler(cur.fetchall(), vals)
                        elif st.key:
                            rows = cur.fetchall()
                            assert rows, f"{tname} {piece} found no row for {st.key}"
                            vals[st.key] = rows[(len(rows) - 1) // 2][0]
                conn.commit()
                if server_s is not None:
                    hold_s = (server_s + (time.perf_counter() - t_resp), wait_s)
                break
            except P.RETRYABLE as e:
                conn.rollback()
                retries += 1
                cause = ("deadlock" if isinstance(e, psycopg2.errors.DeadlockDetected)
                         else "serialization")
                causes[cause] = causes.get(cause, 0) + 1
                if time.perf_counter() > deadline:
                    return None
            except UserRollback:
                conn.rollback()
                return "rollback", retries, causes, hold_s
            except Skip:
                conn.commit()
                return "skip", retries, causes, hold_s
    return "commit", retries, causes, hold_s


def stock_level(conn, w, d, threshold):
    """Below serializability (MODEL.md S7): READ COMMITTED, no explicit locks, not chopped."""
    with conn.cursor() as cur:
        cur.execute("SELECT d_next_o_id FROM district WHERE d_w_id = %s AND d_id = %s", (w, d))
        nxt = cur.fetchone()[0]
        cur.execute("SELECT count(DISTINCT s_i_id) FROM order_line JOIN stock "
                    "ON s_w_id = ol_w_id AND s_i_id = ol_i_id "
                    "WHERE ol_w_id = %s AND ol_d_id = %s AND ol_o_id >= %s AND ol_o_id < %s "
                    "AND s_quantity < %s", (w, d, nxt - 20, nxt, threshold))
        cur.fetchone()
    conn.commit()


@dataclass
class TypeStats:
    done: int = 0
    latencies_s: list = field(default_factory=list)
    retries: int = 0
    rollbacks: int = 0
    skipped: int = 0                               # Delivery: districts without an order


@dataclass
class WorkerResult:
    types: dict = field(default_factory=lambda: {t: TypeStats() for t in TYPES})
    causes: dict = field(default_factory=dict)
    holds_s: list = field(default_factory=list)       # Payment: warehouse-lock hold
    waits_s: list = field(default_factory=list)       # Payment: warehouse-lock wait
    abandoned: int = 0


def worker(i, dsn, arm_plan, prologue, n_wh, window, grace_s, barrier, result, seed):
    conn = psycopg2.connect(dsn)
    conn.set_session(isolation_level="READ COMMITTED", autocommit=False)
    rng = random.Random(seed)
    home_w = i % n_wh + 1
    barrier.wait()
    t0 = time.perf_counter()
    deadline = t0 + window.end_s + grace_s
    while time.perf_counter() - t0 < window.end_s:
        tname = rng.choices(TYPES, MIX_WEIGHTS)[0]
        start = time.perf_counter()
        outs = []
        if tname == "StockLevel":
            stock_level(conn, home_w, rng.randint(1, 10), rng.randint(10, 20))
            outs.append(("commit", 0, {}, None))
        elif tname == "Delivery":
            carrier = rng.randint(1, 10)
            for d in range(1, 11):
                outs.append(run_pieces(conn, tname, arm_plan[tname],
                                       {"w": home_w, "d": d, "carrier": carrier}, deadline,
                                       prologue))
                if outs[-1] is None:
                    break
        else:
            vals = (neworder_params(rng, home_w, n_wh) if tname == "NewOrder"
                    else P.payment_params(rng, home_w, n_wh) if tname == "Payment"
                    else orderstatus_params(rng, home_w))
            outs.append(run_pieces(conn, tname, arm_plan[tname], vals, deadline, prologue))
        if outs[-1] is None:
            result.abandoned += 1
            break
        if not window.contains(time.perf_counter() - t0):
            continue
        st = result.types[tname]
        st.done += 1
        st.latencies_s.append(time.perf_counter() - start)
        for outcome, retries, causes, hold_s in outs:
            st.retries += retries
            st.rollbacks += outcome == "rollback"
            st.skipped += outcome == "skip"
            for cause, n in causes.items():
                result.causes[cause] = result.causes.get(cause, 0) + n
            if hold_s is not None:
                result.holds_s.append(hold_s[0])
                result.waits_s.append(hold_s[1])
    conn.close()


def client_process(ids, dsn, arm_plan, prologue, n_wh, window, grace_s, barrier, seed, cpus,
                   queue):
    P.pin_cpus(cpus)
    results = [WorkerResult() for _ in ids]
    threads = [threading.Thread(target=worker, args=(i, dsn, arm_plan, prologue, n_wh, window,
                                                     grace_s, barrier, results[k],
                                                     seed * 1000 + i))
               for k, i in enumerate(ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    queue.put(results)


def setup(dsn, n_wh, layout="split"):
    """Load the full TPC-C database; layout="joined" for the R14 check."""
    P.setup(dsn, n_wh, layout, full_load=True)


CC = ["cc1", "cc2", "cc3", "cc4", "cc5", "cc6", "cc7", "cc8", "cc9", "cc10", "cc12"]
FIELDS = (["code_version", "workload", "storage", "fsync", "synchronous_commit", "server_version", "pg_cpus",
           "host", "client_procs", "client_cpus", "cc", "rep", "arm", "warehouses", "clients",
           "seed", "pieces", "district_layout"] + P.WAIT_FIELDS + ["throughput_tps", "tpmC"]
          + [f"tps_{t}" for t in TYPES]
          + [f"{m}_{t}" for t in TYPES for m in ("p50_ms", "p95_ms", "retries")]
          + ["rollbacks_NewOrder", "skipped_districts", "deadlocks", "serialization_failures",
             "wlock_hold_p50_ms", "wlock_hold_p95_ms", "wlock_wait_p50_ms", "wlock_wait_p95_ms",
             "abandoned", "check", "check_s"]
          + [f"{c}_violations" for c in CC] + ["status"])


def full_check(dsn):
    """All checked consistency conditions on the whole database: {condition: violations}."""
    return dict(zip(CC, P.admin(dsn, "SELECT * FROM consistency_mix(true)")))


def run_cell(dsn, arm, n_wh, clients, duration_s=30.0, warmup_s=2.0, grace_s=10.0, seed=0,
             procs=1, client_cpus="", check="full"):
    """One measurement point of the full mix. Expects setup() for n_wh to have run."""
    arm_plan = {t: ps for t, ps in plan(arm).items()}
    prologue = P.prologue_for(arm)
    variant = {"B-inc": "inc", "B-rec": "rec_lock"}[arm.split("/")[0]]
    P.admin(dsn, f"SELECT set_cpr('{variant}')",
            "VACUUM ANALYZE warehouse, district, district_ytd, new_order")
    window = P.MeasurementWindow(warmup_s, warmup_s + duration_s)
    sampler = P.WaitSampler(dsn, clients, warmup_s, warmup_s + duration_s)
    sampler.start()
    cpus = P.parse_cpus(client_cpus)
    procs = max(1, min(procs, clients))
    if procs == 1:
        applied = P.pin_cpus(cpus)
        barrier = threading.Barrier(clients)
        results = [WorkerResult() for _ in range(clients)]
        threads = [threading.Thread(target=worker, args=(i, dsn, arm_plan, prologue, n_wh, window,
                                                         grace_s, barrier, results[i],
                                                         seed * 1000 + i))
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
                          args=(ids, dsn, arm_plan, prologue, n_wh, window, grace_s, barrier,
                                seed, cpus, queue))
              for ids in shares]
        for pr in ps:
            pr.start()
        results = [r for _ in ps for r in queue.get()]
        for pr in ps:
            pr.join()
        applied = ",".join(map(str, sorted(cpus)))

    sampler.join()
    t_check = time.perf_counter()
    violations = dict(zip(CC, P.admin(dsn, f"SELECT * FROM consistency_mix({check == 'full'})")))
    check_s = time.perf_counter() - t_check
    quantile = lambda xs, f: xs[min(int(f * len(xs)), len(xs) - 1)] * 1000.0 if xs else float("nan")
    row = {"code_version": P.CODE_VERSION, "workload": "mix", "cc": "2pl", "arm": arm, "warehouses": n_wh, "clients": clients,
           "seed": seed, "client_procs": procs, "client_cpus": applied,
           "host": socket.gethostname(),
           "pieces": " ".join(f"{t}={len(ps)}" for t, ps in arm_plan.items()),
           "district_layout": P.district_layout(dsn), **sampler.fractions()}
    total = 0
    for t in TYPES:
        sts = [r.types[t] for r in results]
        done = sum(s.done for s in sts)
        lat = sorted(x for s in sts for x in s.latencies_s)
        total += done
        row[f"tps_{t}"] = done / window.length_s
        row[f"p50_ms_{t}"], row[f"p95_ms_{t}"] = quantile(lat, 0.5), quantile(lat, 0.95)
        row[f"retries_{t}"] = sum(s.retries for s in sts) / done if done else float("nan")
    row["throughput_tps"] = total / window.length_s
    row["tpmC"] = row["tps_NewOrder"] * 60
    row["rollbacks_NewOrder"] = sum(r.types["NewOrder"].rollbacks for r in results)
    row["skipped_districts"] = sum(r.types["Delivery"].skipped for r in results)
    causes = {}
    for r in results:
        for c, n in r.causes.items():
            causes[c] = causes.get(c, 0) + n
    row["deadlocks"] = causes.get("deadlock", 0)
    row["serialization_failures"] = causes.get("serialization", 0)
    holds = sorted(x for r in results for x in r.holds_s)
    row["wlock_hold_p50_ms"], row["wlock_hold_p95_ms"] = quantile(holds, 0.5), quantile(holds, 0.95)
    waits = sorted(x for r in results for x in r.waits_s)
    row["wlock_wait_p50_ms"], row["wlock_wait_p95_ms"] = quantile(waits, 0.5), quantile(waits, 0.95)
    row["abandoned"] = sum(r.abandoned for r in results)
    row["check_s"] = check_s
    row["check"] = check
    row.update({f"{c}_violations": v for c, v in violations.items()})
    ok = total and not row["abandoned"] and not any(violations.values())
    row["status"] = "OK" if ok else "FLAG"
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=P.DSN_TMPFS)
    ap.add_argument("--plan", action="store_true", help="print each arm's pieces and exit")
    ap.add_argument("--setup", action="store_true", help="load the full database, then exit")
    ap.add_argument("--arm", choices=ARMS)
    ap.add_argument("--procs", type=int, default=1)
    ap.add_argument("--client-cpus", default="")
    ap.add_argument("--warehouses", type=int, default=1)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--duration-s", type=float, default=30.0)
    ap.add_argument("--warmup-s", type=float, default=2.0)
    args = ap.parse_args()

    if args.plan:
        for arm in ARMS:
            print(arm)
            for t, ps in plan(arm).items():
                print(f"  {t:12s} " + "  ".join(
                    "{" + "+".join(p) + "}" + ("[" + ",".join(
                        f"{TABLE_LOCKS_BY_CLASS[c]}:{m}" for c, m in locks) + "]" if locks else "")
                    for p, locks in ps))
        print("  [table:X/S] = district table lock, exclusive/shared (MODEL.md R13)")
        return
    if args.setup:
        t = time.perf_counter()
        setup(args.dsn, args.warehouses)
        print(f"loaded {args.warehouses} warehouse(s) in {time.perf_counter() - t:.0f} s")
        return
    if not args.arm:
        ap.error("--arm is required unless --plan or --setup")
    row = run_cell(args.dsn, args.arm, args.warehouses, args.clients, args.duration_s,
                   args.warmup_s, procs=args.procs, client_cpus=args.client_cpus)
    for k, v in row.items():
        print(f"{k:26s} {v}")


TABLE_LOCKS_BY_CLASS = {cls: table for cls, table in TABLE_LOCKS.values()}

if __name__ == "__main__":
    main()

"""
Minimal FineChop + CPR-aware FineChop on SmallBank and TPC-C templates.

Model (deliberately simple):
- An operation accesses (relation, attribute) cells. Tuple identity is ignored
  (conservative: any two ops on the same relation/attribute may hit the same tuple).
- Kinds: R (read), W (write), U (read-modify-write). U may be flagged commutative:
  an increment x += d whose new value is not read or returned (d may depend on
  earlier reads; those reads conflict on their own).
- Two ops conflict if they share a (rel, attr), at least one writes it,
  and they are not both commutative U's.
- Every template runs as two instances (#1, #2).
- A CPR fires on a write to (rel, attr) and adds triggered ops to the projection
  of the triggering op. Conflicts are tested on projections.
- `uses` lists the ops whose values an op consumes (data flow); `after` lists ops
  it must follow for other reasons (e.g. an ordering the spec requires). Together
  with conflicting ops in program order, they form the dependency graph; its
  cycles are merged, and pieces run in a topological order of it.
- Templates flagged `weak` (allowed to run below serializability) are excluded
  from the conflict analysis and not chopped.
- `rollback` lists the ops that decide a rollback. Before the analysis, each
  template is rewritten so these ops occur as early as possible (Shasha's advice
  to move rollbacks forward): a rollback op moves before an op only if it does not
  depend on it (`uses`/`after`) and their projections do not conflict. On the rewritten template,
  rollback-safety is Shasha's strict rule: the first piece contains the rollback
  ops, every write before them, and every piece that must run before them in the
  dependency graph (which covers `uses`/`after`).
"""
from itertools import combinations
import networkx as nx

# ---------------------------------------------------------------- operations
def op(oid, kind, rel, *attrs, comm=False, uses=(), after=()):
    return dict(id=oid, kind=kind, rel=rel, attrs=set(attrs), comm=comm,
                uses=set(uses), after=set(after))

def tpl(*ops, rollback=(), weak=False):
    return dict(ops=list(ops), rollback=set(rollback), weak=weak)

def deps(o):
    """Ops that `o` must follow: data flow plus explicit ordering."""
    return o["uses"] | o["after"]

def writes(o):
    return o["kind"] in ("W", "U")

def conf_op(a, b):
    if a["rel"] != b["rel"] or not (a["attrs"] & b["attrs"]):
        return False
    if not (writes(a) or writes(b)):
        return False
    if a["kind"] == "U" and b["kind"] == "U" and a["comm"] and b["comm"]:
        return False
    return True

def conf(S, T):
    return any(conf_op(a, b) for a in S for b in T)

def validate(templates):
    for name, t in templates.items():
        seen = set()
        for o in t["ops"]:
            assert o["id"] not in seen, f"{name}: duplicate op {o['id']}"
            assert deps(o) <= seen, f"{name}: {o['id']} depends on a later or unknown op"
            assert not o["comm"] or o["kind"] == "U", f"{name}: {o['id']} comm on non-U"
            seen.add(o["id"])
        assert t["rollback"] <= seen, f"{name}: unknown rollback op"

# ---------------------------------------------------------------- CPR projection
def projection(o, cprs):
    """o plus all ops it triggers. Triggered writes never re-trigger."""
    out = [o]
    if writes(o):
        for c in cprs:
            if c["rel"] == o["rel"] and c["attrs"] & o["attrs"]:
                out += [dict(t, id=f"{o['id']}>{t['id']}") for t in c["triggered"]]
    return out

# ---------------------------------------------------------------- rollback hoisting
def hoist_rollback(t, cprs):
    """Move each rollback op as early as data flow and intra-transaction conflicts
    allow. Returns the rewritten template and {rollback op: [ops it moved past]}."""
    ops = list(t["ops"])
    hoisted = {}
    for r in [o["id"] for o in ops if o["id"] in t["rollback"]]:
        i = next(k for k, o in enumerate(ops) if o["id"] == r)
        while i > 0:
            prev, cur = ops[i - 1], ops[i]
            if prev["id"] in deps(cur) or conf(projection(prev, cprs),
                                                 projection(cur, cprs)):
                break
            ops[i - 1], ops[i] = cur, prev
            hoisted.setdefault(r, []).append(prev["id"])
            i -= 1
    return dict(t, ops=ops), hoisted

# ---------------------------------------------------------------- FineChop
def conflict_components(name, templates, cprs):
    """Conflict components of instance #1's ops, all other transactions unchopped
    (incl. the second copy of `name`). Returns (components, conflicting op ids)."""
    pieces = {o["id"]: projection(o, cprs) for o in templates[name]["ops"]}
    others = {f"{t}#{i}": [x for o in T["ops"] for x in projection(o, cprs)]
              for t, T in templates.items() for i in (1, 2)
              if not (t == name and i == 1) and not T["weak"]}
    G = nx.Graph()
    G.add_nodes_from(("p", k) for k in pieces)
    G.add_nodes_from(("T", k) for k in others)
    for a, pa in pieces.items():          # no C-edges between siblings
        for b, tb in others.items():
            if conf(pa, tb):
                G.add_edge(("p", a), ("T", b))
    for a, b in combinations(others, 2):
        if conf(others[a], others[b]):
            G.add_edge(("T", a), ("T", b))
    comps, hot = [], set()
    for comp in nx.connected_components(G):
        ids = {k for kind, k in comp if kind == "p"}
        if ids:
            comps.append(ids)
            if any(kind == "T" for kind, _ in comp):
                hot |= ids
    return comps, hot

def dependency_graph(pieces, ops, cprs):
    """Edge p -> q if an op of p precedes a conflicting op of q, or an op of q depends on p.
    Triggered ops inherit their trigger's position (lifted order): precedence is
    tested on original ops, conflicts on their projections."""
    pos = {o["id"]: i for i, o in enumerate(ops)}
    proj = {o["id"]: projection(o, cprs) for o in ops}
    piece_of = {x: i for i, p in enumerate(pieces) for x in p}
    D = nx.DiGraph()
    D.add_nodes_from(range(len(pieces)))
    for a in ops:
        for b in ops:
            i, j = piece_of[a["id"]], piece_of[b["id"]]
            if i == j:
                continue
            if (pos[a["id"]] < pos[b["id"]] and conf(proj[a["id"]], proj[b["id"]])) \
                    or a["id"] in deps(b):
                D.add_edge(i, j)
    return D

def fine_chop(name, templates, cprs):
    """Finest chopping of `name`: list of (op ids, conflicting, is_first) in execution order."""
    t = templates[name]
    ops, rollback = t["ops"], t["rollback"]
    pos = {o["id"]: i for i, o in enumerate(ops)}
    pieces, hot = conflict_components(name, templates, cprs)
    first = None
    while True:
        D = dependency_graph(pieces, ops, cprs)
        sccs = list(nx.strongly_connected_components(D))
        if len(sccs) < len(pieces):                       # merge dependency cycles
            pieces = [set().union(*(pieces[i] for i in s)) for s in sccs]
            continue
        if rollback:                                      # rollback-safety (strict)
            last = max(pos[r] for r in rollback)
            need = set(rollback) | {o["id"] for o in ops
                                    if writes(o) and pos[o["id"]] < last}
            idx = {i for i, p in enumerate(pieces) if p & need}
            idx |= set().union(*(nx.ancestors(D, i) for i in idx))
            first = set().union(*(pieces[i] for i in idx))
            if len(idx) > 1:
                pieces = [first] + [p for i, p in enumerate(pieces) if i not in idx]
                continue
        break
    key = lambda i: (pieces[i] != first, min(pos[x] for x in pieces[i]))
    order = nx.lexicographical_topological_sort(D, key=key)
    return [(sorted(pieces[i], key=pos.get), bool(pieces[i] & hot), pieces[i] == first)
            for i in order]

# ---------------------------------------------------------------- SmallBank
# PVLDB 2021 version (Vandevoort et al.): five templates, no SendPayment, no aborts
# (TransactSavings is a plain increment; WriteCheck's overdraft applies a penalty).
SMALLBANK = {
    "Balance":         tpl(op("b1", "R", "Account", "name", "custid"),
                           op("b2", "R", "Savings", "bal", uses=["b1"]),
                           op("b3", "R", "Checking", "bal", uses=["b1"])),
    "DepositChecking": tpl(op("d1", "R", "Account", "name", "custid"),
                           op("d2", "U", "Checking", "bal", comm=True, uses=["d1"])),
    "TransactSavings": tpl(op("t1", "R", "Account", "name", "custid"),
                           op("t2", "U", "Savings", "bal", comm=True, uses=["t1"])),
    "Amalgamate":      tpl(op("a1", "R", "Account", "name", "custid"),
                           op("a2", "R", "Account", "name", "custid"),
                           op("a3", "U", "Savings", "bal", uses=["a1"]),     # read, set to 0
                           op("a4", "U", "Checking", "bal", uses=["a1"]),    # read, set to 0
                           op("a5", "U", "Checking", "bal", comm=True,       # += a + b
                              uses=["a2", "a3", "a4"])),
    "WriteCheck":      tpl(op("w1", "R", "Account", "name", "custid"),
                           op("w2", "R", "Savings", "bal", uses=["w1"]),
                           op("w3", "R", "Checking", "bal", uses=["w1"]),
                           op("w4", "U", "Checking", "bal", comm=True,       # -= V or V+1
                              uses=["w1", "w2", "w3"])),
}

# ---------------------------------------------------------------- TPC-C (simplified)
# Spec order (TPC-C rev. 5.11, clauses 2.4-2.8). NewOrder's item loop is unrolled
# to two iterations: item a is valid, item b is the last item and may be unused.
# The spec requires all valid items to be processed before the unused one, so
# the rollback check on b stays `after` item a. Delivery's loop is one district,
# which the spec allows to run as its own transaction (up to 10 per Delivery).
NEWORDER = tpl(
    op("no1", "R", "Warehouse", "w_tax"),
    op("no2", "R", "District", "d_tax"),
    op("no3", "U", "District", "d_next_o_id"),                      # value is used
    op("no4", "R", "Customer", "c_discount", "c_last", "c_credit"),
    op("no5", "W", "Order", "o_row", "o_carrier_id", uses=["no3"]),   # insert
    op("no6", "W", "NewOrder", "no_row", uses=["no3"]),               # insert
    # item a (valid)
    op("no7a", "R", "Item", "i_price"),
    op("no8a", "U", "Stock", "s_quantity"),                         # conditional, displayed
    op("no9a", "U", "Stock", "s_ytd", comm=True),
    op("no10a", "U", "Stock", "s_order_cnt", "s_remote_cnt", comm=True),
    op("no11a", "W", "OrderLine", "ol_row", "ol_delivery_d", uses=["no3", "no7a", "no8a"]),
    # item b (last item, may be unused -> rollback)
    op("no7b", "R", "Item", "i_price",
       after=["no7a", "no8a", "no9a", "no10a", "no11a"]),
    op("no8b", "U", "Stock", "s_quantity"),
    op("no9b", "U", "Stock", "s_ytd", comm=True),
    op("no10b", "U", "Stock", "s_order_cnt", "s_remote_cnt", comm=True),
    op("no11b", "W", "OrderLine", "ol_row", "ol_delivery_d", uses=["no3", "no7b", "no8b"]),
    rollback=["no7b"])

TPCC = {
    "NewOrder": NEWORDER,
    "Payment": tpl(
        op("pa1", "U", "Warehouse", "w_ytd", comm=True),
        op("pa2", "R", "Warehouse", "w_name"),
        op("pa3", "U", "District", "d_ytd", comm=True),
        op("pa4", "R", "District", "d_name"),
        # customer by id (40%) or by last name (60%, a predicate read); always read, since
        # the spec displays the customer's name either way. pa5-pa7 use the id it resolves.
        op("pc", "R", "Customer", "c_id", "c_first", "c_last"),
        op("pa5", "U", "Customer", "c_balance", "c_data", uses=["pc"]),  # displayed; c_data for BC
        op("pa6", "U", "Customer", "c_ytd_payment", "c_payment_cnt", comm=True, uses=["pc"]),
        op("pa7", "W", "History", "h_row", uses=["pa2", "pa4", "pc"])),  # h_data = w_name+d_name
    "OrderStatus": tpl(
        # customer by id or by last name, as in Payment (clause 2.6.2.2)
        op("osc", "R", "Customer", "c_id", "c_first", "c_last"),
        op("os1", "R", "Customer", "c_balance", uses=["osc"]),
        op("os2", "R", "Order", "o_row", "o_carrier_id", uses=["osc"]),
        op("os3", "R", "OrderLine", "ol_row", "ol_delivery_d", uses=["os2"])),
    "Delivery": tpl(
        op("de1", "R", "NewOrder", "no_row"),
        op("de2", "W", "NewOrder", "no_row", uses=["de1"]),          # delete
        op("de3", "W", "Order", "o_carrier_id", uses=["de1"]),
        op("de4", "W", "OrderLine", "ol_delivery_d", uses=["de1"]),
        op("de5", "R", "OrderLine", "ol_row", uses=["de1"]),
        op("de6", "U", "Customer", "c_balance", comm=True, uses=["de3", "de5"]),  # += sum(ol_amount)
        op("de7", "U", "Customer", "c_delivery_cnt", comm=True, uses=["de3"])),  # de3 gives o_c_id
    # The spec requires neither serializability nor repeatable reads for Stock-Level.
    "StockLevel": tpl(
        op("sl1", "R", "District", "d_next_o_id"),
        op("sl2", "R", "OrderLine", "ol_row", uses=["sl1"]),
        op("sl3", "R", "Stock", "s_quantity", uses=["sl2"]),
        weak=True),
}

# ---------------------------------------------------------------- CPR scenarios
# A scenario adds/replaces templates and adds CPRs. Scenarios with templates are
# also run as "<name>-templates" (templates, no CPRs) to isolate the CPR effect.
def cpr(rel, attr, *triggered):
    return dict(rel=rel, attrs={attr}, triggered=list(triggered))

# Payment without its explicit w_ytd update; in scenario B a CPR maintains it.
PAYMENT_NO_WYTD = dict(TPCC["Payment"],
                       ops=[o for o in TPCC["Payment"]["ops"] if o["id"] != "pa1"])

TPCC_SCENARIOS = {
    # private mirror of c_balance in a billing system
    "A":     dict(cprs=[cpr("Customer", "c_balance",
                            op("bill", "W", "Billing", "balance"))]),
    # Consistency condition 1: W_YTD = sum(D_YTD). The duplicated aggregate is
    # maintained by a CPR on d_ytd instead of Payment's explicit update.
    # B-inc: add the delta to w_ytd (keeps commutativity).
    "B-inc": dict(group="B", templates={"Payment": PAYMENT_NO_WYTD},
                  cprs=[cpr("District", "d_ytd",
                            op("wy", "U", "Warehouse", "w_ytd", comm=True))]),
    # B-rec: recompute w_ytd from all districts' d_ytd (breaks commutativity).
    "B-rec": dict(group="B", templates={"Payment": PAYMENT_NO_WYTD},
                  cprs=[cpr("District", "d_ytd",
                            op("rd", "R", "District", "d_ytd"),
                            op("rw", "W", "Warehouse", "w_ytd"))]),
    # The spec notes I_PRICE is conceivably modified from time to time in a larger
    # application (isolation test 7 raises it). Here a product information system
    # pushes prices into Item.
    "D":     dict(templates={"UpdatePrice": tpl(op("up1", "U", "Catalog", "price"))},
                  cprs=[cpr("Catalog", "price", op("ip", "W", "Item", "i_price"))]),
    # SYNTHETIC (not grounded in the spec): tax rates are set externally and change
    # over time, so a tax-rate model pushes district tax rates into District.d_tax.
    "D'":    dict(synthetic=True,
                  templates={"UpdateTax": tpl(op("ut1", "U", "TaxRate", "rate"))},
                  cprs=[cpr("TaxRate", "rate", op("dt", "W", "District", "d_tax"))]),
}

SMALLBANK_SCENARIOS = {
    "A": dict(cprs=[cpr("Checking", "bal", op("mir", "W", "CheckingMirror", "bal"))]),
    # CRM system pushes name changes into the read-only Account table
    "D": dict(templates={"UpdateName": tpl(op("un1", "U", "CRM", "name"))},
              cprs=[cpr("CRM", "name", op("crm", "W", "Account", "name"))]),
}

# ---------------------------------------------------------------- report
def show(piece):
    ids, conflicting, is_first = piece
    return ("R:" if is_first else "") + "{" + ",".join(ids) + "}" + ("" if conflicting else "°")

def configurations(scenarios):
    """(name, extra templates, cprs, reference config for the * marker).
    Scenarios sharing a `group` share one templates-only run."""
    yield "none", {}, [], None
    done = set()
    for sc, s in scenarios.items():
        extra, cprs = s.get("templates", {}), s.get("cprs", [])
        if extra:
            ref = f"{s.get('group', sc)}-templates"
            if ref not in done:
                done.add(ref)
                yield ref, extra, [], "none"
            yield sc, extra, cprs, ref
        else:
            yield sc, extra, cprs, "none"

def chop_config(templates, cprs):
    """Hoist and chop every template.
    Returns {name: (pieces, hoisted, blocked)}; blocked = a CPR prevented a hoist."""
    hoist = {n: hoist_rollback(t, cprs) for n, t in templates.items()}
    blocked = {n: hoist[n][1] != hoist_rollback(t, [])[1] for n, t in templates.items()}
    rewritten = {n: h[0] for n, h in hoist.items()}
    return {n: (fine_chop(n, rewritten, cprs), hoist[n][1], blocked[n])
            for n, t in rewritten.items() if not t["weak"]}

def metrics(pieces, t, cprs):
    """(free hot increments incl. triggered ones, free reads, |contended piece| in
    original ops, triggered ops in the contended piece)"""
    byid = {o["id"]: o for o in t["ops"]}
    free = [byid[x] for ids, hot, _ in pieces if not hot for x in ids]
    contended = [byid[x] for ids, hot, _ in pieces if hot for x in ids]
    return (sum(x["kind"] == "U" and x["comm"] for o in free for x in projection(o, cprs)),
            sum(o["kind"] == "R" for o in free),
            len(contended),
            sum(len(projection(o, cprs)) - 1 for o in contended))

def free_increments(pieces, t, cprs):
    """Names (rel.attrs) of the free hot increments, incl. triggered ones."""
    byid = {o["id"]: o for o in t["ops"]}
    return {f"{x['rel']}.{'+'.join(sorted(x['attrs']))}"
            for ids, hot, _ in pieces if not hot for i in ids
            for x in projection(byid[i], cprs) if x["kind"] == "U" and x["comm"]}

# Rows per warehouse in TPC-C (clause 1.2): fewer rows = hotter lock.
TPCC_ROWS = {"Warehouse": 1, "District": 10, "Customer": 30_000, "Stock": 100_000}

def run(bench, templates, scenarios, subset=None, verbose=False, triggered=False, rows=None):
    base = {k: v for k, v in templates.items() if subset is None or k in subset}
    title = bench + ("" if subset is None else f"  subset {sorted(subset)}")
    print(f"\n=== {title}")
    results, refs, used = {}, {}, {}
    for sc, extra, cprs, ref in configurations(scenarios):
        tpls = {**base, **extra}
        validate(tpls)
        results[sc], refs[sc], used[sc] = chop_config(tpls, cprs), ref, (tpls, cprs)
        if verbose:
            print(f"-- {sc}   (° = conflict-free piece, R: = rollback-safe first piece)")
            for name, (pieces, hoisted, blocked) in results[sc].items():
                for r, past in hoisted.items():
                    print(f"   (hoisted {name}.{r} before {','.join(reversed(past))})")
                print(f"   {name:16s} {len(pieces)} pieces: " + " ".join(map(show, pieces))
                      + ("   [CPR blocked a hoist]" if blocked else ""))
    # table: rows = templates, columns = configurations
    names = list(dict.fromkeys(n for r in results.values() for n in r))
    weak = [n for n, t in base.items() if t["weak"]]
    cell = lambda sc, n: (metrics(results[sc][n][0], used[sc][0][n], used[sc][1])
                          if n in results[sc] else None)
    w = max(9, *(len(sc) + 1 for sc in results))
    print(f"   {'template':16s}" + "".join(f"{sc:>{w}s}" for sc in results))
    for n in names:
        row = ""
        for sc in results:
            m = cell(sc, n)
            if m is None:
                row += f"{'-':>{w}s}"
                continue
            ref = refs[sc]
            mark = "*" if ref and n in results[ref] and cell(ref, n)[2] != m[2] else ""
            mark += "†" if results[sc][n][2] else ""
            text = "/".join(map(str, m[:3])) + (f"+{m[3]}" if triggered else "")
            row += f"{text + mark:>{w}s}"
        print(f"   {n:16s}{row}")
    for n in weak:
        print(f"   {n:16s}excluded (spec allows it to run below serializability)")
    print("   cell = free hot increments / free reads / |contended piece|"
          + (" + triggered ops in the contended piece" if triggered else "") + ".")
    print("   Free hot increments count CPR-triggered increments too; the other columns"
          " count original operations only.")
    print("   * = merge: contended piece differs from its reference (none, or X-templates);"
          " † = a CPR blocked a rollback hoist (merge from rollback-safety, not conflict)")
    syn = [sc for sc, s in scenarios.items() if s.get("synthetic")]
    if syn:
        print(f"   synthetic (not grounded in the benchmark spec): {', '.join(syn)}")
    if rows is None:
        return
    # name the free hot increments that changed against "none" (templates-only runs
    # are controls for merges and skipped), hottest first
    hot = lambda c: (rows.get(c.split(".")[0], float("inf")), c)
    fmt = lambda cs: ", ".join(f"{c} ({rows.get(c.split('.')[0], '?')}/wh)"
                               for c in sorted(cs, key=hot))
    notes = []
    for sc in results:
        if sc == "none" or sc.endswith("-templates"):
            continue
        for n in results[sc]:
            if n not in results["none"]:
                continue
            now = free_increments(results[sc][n][0], used[sc][0][n], used[sc][1])
            before = free_increments(results["none"][n][0], used["none"][0][n], [])
            if now != before:
                line = f"   {n} @ {sc}:"
                if before - now:
                    line += f" no longer free: {fmt(before - now)};"
                if now - before:
                    line += f" newly free: {fmt(now - before)};"
                if now & before:
                    line += f" still free: {fmt(now & before)}"
                notes.append(line.rstrip(";"))
    if notes:
        print("   Free hot increments that changed against none (rows per warehouse, hottest first):")
        print("\n".join(notes))

if __name__ == "__main__":
    import sys
    v, t = "-v" in sys.argv, "-t" in sys.argv
    run("SmallBank", SMALLBANK, SMALLBANK_SCENARIOS, verbose=v, triggered=t)
    for subset in ({"Balance", "DepositChecking"}, {"Balance", "TransactSavings"},
                   {"DepositChecking", "TransactSavings", "Amalgamate"}):
        run("SmallBank", SMALLBANK, SMALLBANK_SCENARIOS, subset, verbose=v, triggered=t)
    run("TPC-C", TPCC, TPCC_SCENARIOS, verbose=v, triggered=t, rows=TPCC_ROWS)

-- TPC-C schema for the runtime check, shared by both workloads:
-- Payment only (payment.py) and the full mix (mix.py).
--
-- No foreign keys: FK checks take KEY SHARE locks on the parent rows (warehouse, district),
-- which would add a lock protocol the analysis does not model. TPC-C does not require them.
--
-- District is split vertically (MODEL.md R14): d_ytd lives in district_ytd. Postgres locks whole
-- rows, and the analysis treats d_ytd (Payment, the CPR) and d_next_o_id (NewOrder) as separate
-- cells. In one row, Payment's d_ytd update would wait for NewOrder's long first piece, while
-- already holding the warehouse lock (pre-lock rule, R3): a wait the analysis does not predict.
-- TPC-C allows vertical partitioning (clauses 1.5.2, 1.6). join_district() restores the stock
-- layout for the check that quantifies this choice (MODEL.md R14, server stage `layout`).

DROP TABLE IF EXISTS district CASCADE;      -- also drops the district_ytd view (joined layout)
DROP TABLE IF EXISTS history, customer, district_ytd, warehouse,
                     orders, new_order, order_line, item, stock CASCADE;

CREATE TABLE warehouse (
    w_id     int PRIMARY KEY,
    w_name   varchar(10) NOT NULL,
    w_street varchar(20) NOT NULL,
    w_city   varchar(20) NOT NULL,
    w_tax    numeric(4,4) NOT NULL,
    w_ytd    numeric(14,2) NOT NULL
);

CREATE TABLE district (
    d_w_id      int NOT NULL,
    d_id        int NOT NULL,
    d_name      varchar(10) NOT NULL,
    d_street    varchar(20) NOT NULL,
    d_city      varchar(20) NOT NULL,
    d_tax       numeric(4,4) NOT NULL,
    d_next_o_id int NOT NULL,
    PRIMARY KEY (d_w_id, d_id)
);

CREATE TABLE district_ytd (
    d_w_id int NOT NULL,
    d_id   int NOT NULL,
    d_ytd  numeric(14,2) NOT NULL,
    PRIMARY KEY (d_w_id, d_id)
);

CREATE TABLE customer (
    c_w_id         int NOT NULL,
    c_d_id         int NOT NULL,
    c_id           int NOT NULL,
    c_first        varchar(16) NOT NULL,
    c_middle       char(2) NOT NULL,
    c_last         varchar(16) NOT NULL,
    c_street       varchar(20) NOT NULL,
    c_city         varchar(20) NOT NULL,
    c_since        timestamp NOT NULL,
    c_credit       char(2) NOT NULL,
    c_credit_lim   numeric(12,2) NOT NULL,
    c_discount     numeric(4,4) NOT NULL,
    c_balance      numeric(14,2) NOT NULL,
    c_ytd_payment  numeric(14,2) NOT NULL,
    c_payment_cnt  int NOT NULL,
    c_delivery_cnt int NOT NULL,
    c_data         varchar(500) NOT NULL,
    PRIMARY KEY (c_w_id, c_d_id, c_id)
);
-- Lookup by last name (Payment, OrderStatus); TPC-C allows this secondary index.
CREATE INDEX customer_name ON customer (c_w_id, c_d_id, c_last, c_first);

-- No primary key, as in the spec. bigserial only gives rows an identity; sequences are
-- non-transactional, so h_id adds no lock between concurrent inserts.
CREATE TABLE history (
    h_id     bigserial,
    h_c_id   int NOT NULL,
    h_c_d_id int NOT NULL,
    h_c_w_id int NOT NULL,
    h_d_id   int NOT NULL,
    h_w_id   int NOT NULL,
    h_date   timestamp NOT NULL,
    h_amount numeric(6,2) NOT NULL,
    h_data   varchar(24) NOT NULL
);

CREATE TABLE orders (
    o_w_id       int NOT NULL,
    o_d_id       int NOT NULL,
    o_id         int NOT NULL,
    o_c_id       int NOT NULL,
    o_entry_d    timestamp NOT NULL,
    o_carrier_id int,
    o_ol_cnt     int NOT NULL,
    o_all_local  int NOT NULL,
    PRIMARY KEY (o_w_id, o_d_id, o_id)
);
-- OrderStatus: the customer's most recent order.
CREATE INDEX orders_customer ON orders (o_w_id, o_d_id, o_c_id, o_id);

CREATE TABLE new_order (
    no_w_id int NOT NULL,
    no_d_id int NOT NULL,
    no_o_id int NOT NULL,
    PRIMARY KEY (no_w_id, no_d_id, no_o_id)
);

CREATE TABLE order_line (
    ol_w_id        int NOT NULL,
    ol_d_id        int NOT NULL,
    ol_o_id        int NOT NULL,
    ol_number      int NOT NULL,
    ol_i_id        int NOT NULL,
    ol_supply_w_id int NOT NULL,
    ol_delivery_d  timestamp,
    ol_quantity    int NOT NULL,
    ol_amount      numeric(6,2) NOT NULL,
    ol_dist_info   char(24) NOT NULL,
    PRIMARY KEY (ol_w_id, ol_d_id, ol_o_id, ol_number)
);

CREATE TABLE item (
    i_id    int PRIMARY KEY,
    i_im_id int NOT NULL,
    i_name  varchar(24) NOT NULL,
    i_price numeric(5,2) NOT NULL,
    i_data  varchar(50) NOT NULL
);

CREATE TABLE stock (
    s_w_id       int NOT NULL,
    s_i_id       int NOT NULL,
    s_quantity   int NOT NULL,
    s_dist_01    char(24) NOT NULL,
    s_dist_02    char(24) NOT NULL,
    s_dist_03    char(24) NOT NULL,
    s_dist_04    char(24) NOT NULL,
    s_dist_05    char(24) NOT NULL,
    s_dist_06    char(24) NOT NULL,
    s_dist_07    char(24) NOT NULL,
    s_dist_08    char(24) NOT NULL,
    s_dist_09    char(24) NOT NULL,
    s_dist_10    char(24) NOT NULL,
    s_ytd        int NOT NULL,
    s_order_cnt  int NOT NULL,
    s_remote_cnt int NOT NULL,
    s_data       varchar(50) NOT NULL,
    PRIMARY KEY (s_w_id, s_i_id)
);

-- Customer last name from a number 0..999, three syllables (TPC-C clause 4.3.2.3).
CREATE OR REPLACE FUNCTION last_name(n int) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT s[n / 100 + 1] || s[n / 10 % 10 + 1] || s[n % 10 + 1]
    FROM (SELECT ARRAY['BAR', 'OUGHT', 'ABLE', 'PRI', 'PRES',
                       'ESE', 'ANTI', 'CALLY', 'ATION', 'EING'] AS s) t;
$$;

-- NURand(A, x, y) with constant c (TPC-C clause 2.1.6), as in py-tpcc.
CREATE OR REPLACE FUNCTION nurand(a int, x int, y int, c int) RETURNS int LANGUAGE sql AS $$
    SELECT (((floor(random() * (a + 1))::int | (x + floor(random() * (y - x + 1))::int)) + c)
            % (y - x + 1)) + x;
$$;

-- Random integer in [lo, hi] and random string of lo..hi characters (spec "a-string").
CREATE OR REPLACE FUNCTION rint(lo int, hi int) RETURNS int LANGUAGE sql AS $$
    SELECT lo + floor(random() * (hi - lo + 1))::int;
$$;
CREATE OR REPLACE FUNCTION rstr(lo int, hi int) RETURNS text LANGUAGE sql AS $$
    SELECT substr(repeat(md5(random()::text), 16), 1, rint(lo, hi));
$$;
-- Data field with "ORIGINAL" in 10% of rows (clause 4.3.3.1: i_data, s_data).
CREATE OR REPLACE FUNCTION rdata() RETURNS text LANGUAGE sql AS $$
    SELECT CASE WHEN random() < 0.1 THEN overlay(rstr(26, 50) PLACING 'ORIGINAL' FROM 3)
                ELSE rstr(26, 50) END;
$$;

-- Initial population (TPC-C clause 4.3.3.1). Warehouse, district and customer are always
-- loaded: w_ytd = 300000, d_ytd = 30000, so consistency condition 1 holds from the start.
-- Last names: customers 1..1000 of each district get last_name(c_id - 1), so every name exists
-- in every district; the rest get NURand(255, 0, 999) with c_load. 10% of customers have bad
-- credit (BC), whose c_data Payment updates.
-- full_load = false (Payment only): no History; payment.py checks conditions 8 and 9 in delta form
-- against each run's own History (reset_run, consistency_violations).
-- full_load = true (full mix): also item, stock, orders, order lines, new orders and the initial
-- History (one row of 10.00 per customer), so all consistency conditions hold as written.
-- Seeded, so a reload gives the same data.
CREATE OR REPLACE FUNCTION populate(n_wh int, n_cust int DEFAULT 3000, c_load int DEFAULT 0,
                                    full_load boolean DEFAULT false)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM setseed(0.42);
    TRUNCATE history, customer, district_ytd, district, warehouse,
             orders, new_order, order_line, item, stock;
    INSERT INTO warehouse
        SELECT w, 'wh' || w, 'street', 'city', rint(0, 2000) / 10000.0, 300000
        FROM generate_series(1, n_wh) w;
    INSERT INTO district
        SELECT w, d, 'd' || d, 'street', 'city', rint(0, 2000) / 10000.0, n_cust + 1
        FROM generate_series(1, n_wh) w, generate_series(1, 10) d;
    INSERT INTO district_ytd
        SELECT w, d, 30000 FROM generate_series(1, n_wh) w, generate_series(1, 10) d;
    INSERT INTO customer
        SELECT w, d, c, rstr(8, 16), 'OE',
               last_name(CASE WHEN c <= 1000 THEN c - 1
                              ELSE nurand(255, 0, least(999, n_cust - 1), c_load) END),
               'street', 'city', now(),
               CASE WHEN random() < 0.1 THEN 'BC' ELSE 'GC' END, 50000,
               rint(0, 5000) / 10000.0, -10, 10, 1, 0, rstr(300, 500)
        FROM generate_series(1, n_wh) w, generate_series(1, 10) d, generate_series(1, n_cust) c;
    IF NOT full_load THEN
        RETURN;
    END IF;

    INSERT INTO history (h_c_id, h_c_d_id, h_c_w_id, h_d_id, h_w_id, h_date, h_amount, h_data)
        SELECT c_id, c_d_id, c_w_id, c_d_id, c_w_id, now(), 10, rstr(12, 24) FROM customer;
    INSERT INTO item
        SELECT i, rint(1, 10000), rstr(14, 24), rint(100, 10000) / 100.0, rdata()
        FROM generate_series(1, 100000) i;
    INSERT INTO stock
        SELECT w, i, rint(10, 100),
               rstr(24, 24), rstr(24, 24), rstr(24, 24), rstr(24, 24), rstr(24, 24),
               rstr(24, 24), rstr(24, 24), rstr(24, 24), rstr(24, 24), rstr(24, 24),
               0, 0, 0, rdata()
        FROM generate_series(1, n_wh) w, generate_series(1, 100000) i;
    -- o_c_id is a random permutation of the customers of each district.
    INSERT INTO orders
        SELECT w, d, o, c, now(), CASE WHEN o <= 2100 THEN rint(1, 10) END, rint(5, 15), 1
        FROM (SELECT w, d, c, row_number() OVER (PARTITION BY w, d ORDER BY random()) AS o
              FROM generate_series(1, n_wh) w, generate_series(1, 10) d,
                   generate_series(1, n_cust) c) p;
    INSERT INTO order_line
        SELECT o_w_id, o_d_id, o_id, n, rint(1, 100000), o_w_id,
               CASE WHEN o_id <= 2100 THEN o_entry_d END, 5,
               CASE WHEN o_id <= 2100 THEN 0 ELSE rint(1, 999999) / 100.0 END, rstr(24, 24)
        FROM orders, generate_series(1, 15) n WHERE n <= o_ol_cnt;
    INSERT INTO new_order
        SELECT o_w_id, o_d_id, o_id FROM orders WHERE o_id > 2100;
END $$;

-- Stock TPC-C layout, for the R14 check: d_ytd moves back into district, and district_ytd
-- becomes a view on it. Postgres updates such a simple view in place, so every statement stays
-- the same, but now locks the shared district row (NewOrder's d_next_o_id, Payment's d_ytd).
-- Call after populate().
CREATE OR REPLACE FUNCTION join_district() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    DROP TRIGGER IF EXISTS trg_wytd ON district_ytd;
    ALTER TABLE district ADD COLUMN d_ytd numeric(14,2);
    UPDATE district d SET d_ytd = y.d_ytd FROM district_ytd y
     WHERE (y.d_w_id, y.d_id) = (d.d_w_id, d.d_id);
    ALTER TABLE district ALTER COLUMN d_ytd SET NOT NULL;
    DROP TABLE district_ytd;
    CREATE VIEW district_ytd AS SELECT d_w_id, d_id, d_ytd FROM district;
END $$;

-- The table holding d_ytd, which the CPR trigger sits on: district_ytd, or district in the
-- joined layout.
CREATE OR REPLACE FUNCTION cpr_table() RETURNS text LANGUAGE sql AS $$
    SELECT CASE WHEN relkind = 'v' THEN 'district' ELSE 'district_ytd' END
      FROM pg_class WHERE oid = 'district_ytd'::regclass;
$$;

-- The W_YTD CPR (consistency condition 1), as an immediate AFTER UPDATE row trigger: it runs
-- right after the triggering statement, inside the same transaction, i.e. inside the trigger's
-- piece (lifted order, MODEL.md C3). It writes only warehouse, which has no trigger, so the
-- trigger guard (MODEL.md C4) holds.

-- B-inc: add the delta (commutative in the analysis).
CREATE OR REPLACE FUNCTION cpr_wytd_inc() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE warehouse SET w_ytd = w_ytd + (NEW.d_ytd - OLD.d_ytd) WHERE w_id = NEW.d_w_id;
    RETURN NULL;
END $$;

-- B-rec: recompute from all districts of the warehouse (reads d_ytd, breaks commutativity).
CREATE OR REPLACE FUNCTION cpr_wytd_rec() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE warehouse
       SET w_ytd = (SELECT sum(d_ytd) FROM district_ytd WHERE d_w_id = NEW.d_w_id)
     WHERE w_id = NEW.d_w_id;
    RETURN NULL;
END $$;

-- B-rec under strict 2PL: the read of all districts' d_ytd takes share locks, held to commit.
-- (FOR SHARE is not allowed on an aggregate directly, hence the sub-select.) Deadlock-free only
-- together with payment.py's lock prologue, which locks the warehouse row before the district
-- update: a BEFORE trigger cannot do that, since Postgres locks the district row before BEFORE
-- row triggers run.
CREATE OR REPLACE FUNCTION cpr_wytd_rec_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE warehouse
       SET w_ytd = (SELECT sum(s.d_ytd)
                      FROM (SELECT d_ytd FROM district_ytd WHERE d_w_id = NEW.d_w_id
                            FOR SHARE) s)
     WHERE w_id = NEW.d_w_id;
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION set_cpr(variant text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    fn text := CASE variant WHEN 'inc' THEN 'cpr_wytd_inc' WHEN 'rec' THEN 'cpr_wytd_rec'
                            WHEN 'rec_lock' THEN 'cpr_wytd_rec_lock' END;
BEGIN
    IF fn IS NULL THEN
        RAISE EXCEPTION 'unknown CPR variant %', variant;
    END IF;
    EXECUTE format('DROP TRIGGER IF EXISTS trg_wytd ON %I', cpr_table());
    EXECUTE format('CREATE TRIGGER trg_wytd AFTER UPDATE OF d_ytd ON %I '
                   'FOR EACH ROW EXECUTE FUNCTION %I()', cpr_table(), fn);
END $$;

-- Payment only, per-run reset: empty History and restore the ytd columns to their initial
-- values, so the TPC-C consistency conditions can be checked against this run's History alone.
-- Drops the CPR trigger first: otherwise the reset's own district update fires it and shifts
-- w_ytd (call set_cpr() afterwards). The full mix is never reset (mix.py checks conditions on
-- the whole database instead).
CREATE OR REPLACE FUNCTION reset_run() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format('DROP TRIGGER IF EXISTS trg_wytd ON %I', cpr_table());
    TRUNCATE history;
    UPDATE warehouse SET w_ytd = 300000;
    UPDATE district_ytd SET d_ytd = 30000;
END $$;

-- Customer totals before a run; customers are not reset (240k rows per 8 warehouses).
CREATE OR REPLACE FUNCTION customer_totals(OUT cnt bigint, OUT bal numeric, OUT ytd numeric)
LANGUAGE sql AS $$
    SELECT sum(c_payment_cnt), sum(c_balance), sum(c_ytd_payment) FROM customer;
$$;

-- Payment only: violations after a run, given the customer totals from before it. All checks
-- are deltas from the state reset_run() leaves (ytd columns at their initial values, History
-- empty), so they hold at the start of every run. Any nonzero count means a piece was lost or
-- the CPR is broken, and the run is flagged.
--   cc1:  warehouses with w_ytd <> sum(d_ytd)                      (TPC-C condition 1)
--   cc8:  warehouses with w_ytd <> initial + sum(h_amount)         (condition 8)
--   cc9:  districts  with d_ytd <> initial + sum(h_amount)         (condition 9)
--   cust: customer totals that do not match History (payment count, balance, ytd payment)
CREATE OR REPLACE FUNCTION consistency_violations(cnt0 bigint, bal0 numeric, ytd0 numeric,
    OUT cc1 bigint, OUT cc8 bigint, OUT cc9 bigint, OUT cust int)
LANGUAGE plpgsql AS $$
DECLARE
    h_cnt bigint; h_sum numeric; c record;
BEGIN
    SELECT count(*) INTO cc1 FROM warehouse w
     WHERE w.w_ytd <> (SELECT sum(d_ytd) FROM district_ytd d WHERE d.d_w_id = w.w_id);
    SELECT count(*) INTO cc8 FROM warehouse w
     WHERE w.w_ytd <> 300000 + coalesce((SELECT sum(h_amount) FROM history h
                                          WHERE h.h_w_id = w.w_id), 0);
    SELECT count(*) INTO cc9 FROM district_ytd d
     WHERE d.d_ytd <> 30000 + coalesce((SELECT sum(h_amount) FROM history h
                                         WHERE h.h_w_id = d.d_w_id AND h.h_d_id = d.d_id), 0);
    SELECT count(*), coalesce(sum(h_amount), 0) INTO h_cnt, h_sum FROM history;
    SELECT * INTO c FROM customer_totals();
    cust := (c.cnt - cnt0 <> h_cnt)::int + (bal0 - c.bal <> h_sum)::int
          + (c.ytd - ytd0 <> h_sum)::int;
END $$;

-- Full mix: TPC-C consistency conditions 1-10 and 12 (clause 3.3.2) on the whole database, as
-- written (the initial population satisfies them). Each count is the number of warehouses,
-- districts, orders, order lines or customers violating the condition. Condition 11 is left out:
-- it describes the initial population and stops holding once Delivery has run.
-- full_check = false checks only conditions 1, 2, 3, 8 and 9 (the others scan every order line
-- and customer; NULL when skipped): mix.py's light per-run check for large grids.
CREATE OR REPLACE FUNCTION consistency_mix(full_check boolean DEFAULT true,
    OUT cc1 bigint, OUT cc2 bigint, OUT cc3 bigint, OUT cc4 bigint, OUT cc5 bigint,
    OUT cc6 bigint, OUT cc7 bigint, OUT cc8 bigint, OUT cc9 bigint, OUT cc10 bigint,
    OUT cc12 bigint)
LANGUAGE plpgsql AS $$
BEGIN
    SELECT count(*) INTO cc1 FROM warehouse w
     WHERE w.w_ytd <> (SELECT sum(d_ytd) FROM district_ytd d WHERE d.d_w_id = w.w_id);
    SELECT count(*) INTO cc2 FROM district d
     WHERE d.d_next_o_id - 1 <> (SELECT max(o_id) FROM orders
                                  WHERE o_w_id = d.d_w_id AND o_d_id = d.d_id)
        OR d.d_next_o_id - 1 <> coalesce((SELECT max(no_o_id) FROM new_order
                                           WHERE no_w_id = d.d_w_id AND no_d_id = d.d_id),
                                          d.d_next_o_id - 1);
    SELECT count(*) INTO cc3 FROM (
        SELECT 1 FROM new_order GROUP BY no_w_id, no_d_id
        HAVING max(no_o_id) - min(no_o_id) + 1 <> count(*)) t;
    SELECT count(*) INTO cc8 FROM warehouse w
     WHERE w.w_ytd <> coalesce((SELECT sum(h_amount) FROM history WHERE h_w_id = w.w_id), 0);
    SELECT count(*) INTO cc9 FROM district_ytd d
     WHERE d.d_ytd <> coalesce((SELECT sum(h_amount) FROM history
                                 WHERE h_w_id = d.d_w_id AND h_d_id = d.d_id), 0);
    IF NOT full_check THEN
        RETURN;
    END IF;
    SELECT count(*) INTO cc4 FROM (
        SELECT o_w_id, o_d_id, sum(o_ol_cnt) n FROM orders GROUP BY 1, 2) o
      FULL JOIN (SELECT ol_w_id, ol_d_id, count(*) n FROM order_line GROUP BY 1, 2) l
        ON (o.o_w_id, o.o_d_id) = (l.ol_w_id, l.ol_d_id)
     WHERE o.n IS DISTINCT FROM l.n;
    SELECT count(*) INTO cc5 FROM orders o
     WHERE (o.o_carrier_id IS NULL) <> EXISTS (
        SELECT 1 FROM new_order WHERE (no_w_id, no_d_id, no_o_id) = (o.o_w_id, o.o_d_id, o.o_id));
    SELECT count(*) INTO cc6 FROM orders o
      LEFT JOIN (SELECT ol_w_id, ol_d_id, ol_o_id, count(*) n FROM order_line GROUP BY 1, 2, 3) l
        ON (l.ol_w_id, l.ol_d_id, l.ol_o_id) = (o.o_w_id, o.o_d_id, o.o_id)
     WHERE o.o_ol_cnt IS DISTINCT FROM l.n;
    SELECT count(*) INTO cc7 FROM order_line l
      JOIN orders o ON (o.o_w_id, o.o_d_id, o.o_id) = (l.ol_w_id, l.ol_d_id, l.ol_o_id)
     WHERE (l.ol_delivery_d IS NULL) <> (o.o_carrier_id IS NULL);
    WITH ol AS (
        SELECT o_w_id w, o_d_id d, o_c_id c, sum(ol_amount) s
          FROM orders JOIN order_line ON (ol_w_id, ol_d_id, ol_o_id) = (o_w_id, o_d_id, o_id)
         WHERE ol_delivery_d IS NOT NULL GROUP BY 1, 2, 3),
    h AS (SELECT h_c_w_id w, h_c_d_id d, h_c_id c, sum(h_amount) s FROM history GROUP BY 1, 2, 3)
    SELECT count(*) FILTER (WHERE c.c_balance <> coalesce(ol.s, 0) - coalesce(h.s, 0)),
           count(*) FILTER (WHERE c.c_balance + c.c_ytd_payment <> coalesce(ol.s, 0))
      INTO cc10, cc12
      FROM customer c
      LEFT JOIN ol ON (ol.w, ol.d, ol.c) = (c.c_w_id, c.c_d_id, c.c_id)
      LEFT JOIN h ON (h.w, h.d, h.c) = (c.c_w_id, c.c_d_id, c.c_id);
END $$;

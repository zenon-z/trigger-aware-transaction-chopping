#!/usr/bin/env bash
# Server campaign of the paper (experiments E1-E3, MODEL.md R21-R23): one cell at a time on the
# whole machine, every cell on a fresh Postgres instance; arms of a rep interleaved within the cell.
#   ./run_server.sh topology   # first: propose the core split (writes server.env; check it)
#   ./run_server.sh checks     # esmoke, calibrate, overhead, merge, locklog (~2.5 h); read
#                              # their verdicts (checks.py) before the campaign
#   ./run_server.sh campaign   # drift start, e1, e2, drift mid, e3, drift end, figures (~20 h)
#   single stages: esmoke calibrate overhead merge locklog e1 e2 e3 "drift <tag>" figures
# Every grid parameter can be overridden by environment variables (see the defaults below).
# Client and Postgres run on the same machine: Postgres is pinned to PG_CPUSET, the client
# processes to CLIENT_CPUS, so they never share cores.
set -euo pipefail
cd "$(dirname "$0")"

# core split from topology.py, if written (check it; environment variables still override it)
if [ -f server.env ]; then
    while IFS='=' read -r k v; do [ -n "$k" ] && [ -z "${!k:-}" ] && export "$k=$v"; done < server.env
fi
# Python with psycopg2-binary, networkx, matplotlib; default: the venv in ~/ttc-venv if present
if [ -z "${PY:-}" ]; then
    if [ -x "$HOME/ttc-venv/bin/python" ]; then PY="$HOME/ttc-venv/bin/python"; else PY=python3; fi
fi
export PG_CPUSET="${PG_CPUSET:-0-31}"
CLIENT_CPUS="${CLIENT_CPUS:-32-47}"
PROCS="${PROCS:-16}"
DURATION="${DURATION:-30}"
CAMPAIGN_REPS="${CAMPAIGN_REPS:-10}"      # E1-E3 and the merge check
TIMING_EVERY="${TIMING_EVERY:-10}"        # time breakdown for every Nth Payment (overhead check:
                                          # timing every Payment cost up to 6% at 8 warehouses)
PREWARM_S="${PREWARM_S:-30}"              # discarded run per fresh instance (first runs were low)
ARMS4="B-inc/unchopped,B-inc/chopped,B-rec/unchopped,B-rec/chopped"
# E1/E2: plus each trigger's unchopped arm without the pa5+pa6 merge (MODEL.md R15), so every
# cell shows how much of the chopped/unchopped gap is the merge that chopping forfeits
ARMS6="$ARMS4,B-inc/unchopped@nomerge,B-rec/unchopped@nomerge"
export PYTHONUNBUFFERED=1
calarg() {   # the lock-wait baselines, once the calibrate stage has written them
    [ -f calibration.json ] && echo "--calibration calibration.json" || true
}

start() {   # start <service> <cpuset>: a fresh Postgres container, pinned to <cpuset>
    # Plain `docker run` (the server has no compose plugin), with the settings of
    # docker-compose.server.yml; keep the two in step. Every call removes the old container, so
    # a tmpfs service starts with an empty data directory; the disk volume is kept.
    local svc="$1" cpus="$2" name port
    local pgargs=(-c max_connections=1000 -c shared_buffers=16GB)
    local storage=(--tmpfs /var/lib/postgresql/data)
    case "$svc" in
      pg-tmpfs) name=cpr_chop_srv_tmpfs; port=15433 ;;
      pg-tmpfs-locklog)
        name=cpr_chop_srv_tmpfs_locklog; port=15433
        pgargs+=(-c logging_collector=on -c log_lock_waits=on -c deadlock_timeout=1ms
                 -c log_rotation_size=0 -c log_rotation_age=0) ;;
      pg-disk)
        name=cpr_chop_srv_disk; port=15434
        storage=(-v cpr_chop_srv_pgdata:/var/lib/postgresql/data) ;;
      *) echo "unknown service $svc" >&2; exit 1 ;;
    esac
    docker rm -f cpr_chop_srv_tmpfs cpr_chop_srv_tmpfs_locklog cpr_chop_srv_disk \
        >/dev/null 2>&1 || true
    # Host networking: Postgres listens on the host's loopback directly, so client round trips
    # skip Docker's port forwarding (NAT and docker-proxy, which would also use CPU outside the
    # pinned cores). Only on 127.0.0.1, so the server is not reachable from the network.
    if ss -ltn 2>/dev/null | grep -q ":$port "; then
        echo "port $port is already in use on this host" >&2; exit 1
    fi
    pgargs+=(-c port="$port" -c listen_addresses=127.0.0.1)
    docker run -d --name "$name" --network host --cpuset-cpus "$cpus" --shm-size 2g \
        -e POSTGRES_USER=cpr -e POSTGRES_PASSWORD=cpr -e POSTGRES_DB=cpr -e PGPORT="$port" \
        "${storage[@]}" postgres:16.15 postgres "${pgargs[@]}" >/dev/null
    for _ in $(seq 90); do
        # over TCP: the image's init server listens only on the socket, so this succeeds only
        # once the real server is up
        docker exec "$name" pg_isready -h 127.0.0.1 -p "$port" -U cpr -d cpr >/dev/null 2>&1 \
            && return
        sleep 2
    done
    echo "postgres ($svc) not ready" >&2; docker logs --tail 30 "$name" >&2; exit 1
}

sweep() {   # sweep <out.csv> <storage> <pg cpuset label> [extra sweep.py args]
    local out="$1" storage="$2" cpus="$3"; shift 3
    "$PY" sweep.py --storage "$storage" --pg-cpus "$cpus" --procs "$PROCS" \
        --client-cpus "$CLIENT_CPUS" --duration-s "$DURATION" --resume --out "$out" "$@" \
        2>&1 | tee -a "${out%.csv}.log"
}

idle_check() {   # log the machine's state; refuse to run next to another client or container
    { echo "[$(date -Is)] $(uptime)"; who; } | tee -a campaign.log
    if pgrep -f "sweep.py|payment.py|mix.py" >/dev/null; then
        echo "another benchmark client is running; stop it first" | tee -a campaign.log >&2
        exit 1
    fi
    local others
    others=$(docker ps --format '{{.Names}}' | grep -v '^cpr_chop_srv' || true)
    if [ -n "$others" ] && [ -z "${ALLOW_OTHER_CONTAINERS:-}" ]; then
        echo "other containers are running: $others (ALLOW_OTHER_CONTAINERS=1 to proceed)" \
            | tee -a campaign.log >&2
        exit 1
    fi
}

cell() {   # cell <out.csv> <experiment> <warehouses> <clients> <reps> <service> [sweep args]
    # one cell on a fresh Postgres instance (new container, empty tmpfs data dir)
    local out="$1" exp="$2" wh="$3" cl="$4" reps="$5" svc="$6"; shift 6
    idle_check
    echo "[$(date -Is)] cell $exp wh=$wh clients=$cl reps=$reps $svc $*" | tee -a campaign.log
    start "$svc" "$PG_CPUSET"
    sweep "$out" tmpfs "$PG_CPUSET" --experiment "$exp" --warehouses "$wh" --clients "$cl" \
        --reps "$reps" --timing-every "$TIMING_EVERY" --prewarm-s "$PREWARM_S" "$@"
}

each() {   # each "1,2,4" -> 1 2 4
    echo "${1//,/ }"
}

case "${1:-}" in
  topology)
    "$PY" topology.py
    command -v numactl >/dev/null && numactl -H || true
    echo "Check server.env; run_server.sh reads it (environment variables override it)."
    ;;
  esmoke)
    # correctness of the new arms and columns: all OK, 0 deadlocks/violations, timing columns
    # filled; about 15 min
    DURATION=10 cell results_server_esmoke.csv esmoke 1 1,32 1 pg-tmpfs --arms "$ARMS4"
    DURATION=10 cell results_server_esmoke.csv esmoke 8 32 1 pg-tmpfs --arms "$ARMS4"
    DURATION=5 cell results_server_esmoke_e3.csv esmoke 1 32 1 pg-tmpfs --contiguous
    "$PY" summarize.py results_server_esmoke*.csv
    ;;
  calibrate)
    # hi baseline: 1 client on 8 warehouses; lo baselines: c clients on c warehouses
    for c in 1 $(each "${CAL_LO_CLIENTS:-8,32,64}"); do
        wh=$c; [ "$c" = 1 ] && wh=8
        cell results_server_calib.csv calib "$wh" "$c" "${CAL_REPS:-5}" pg-tmpfs --arms "$ARMS4"
    done
    "$PY" calibrate.py results_server_calib_stmts.jsonl --out calibration.json
    ;;
  overhead)
    # timing on vs. off (@notiming), interleaved; pass: < 2% or inside the CI
    nt="${ARMS4//,/@notiming,}@notiming"
    for wh in 1 8; do
        cell results_server_overhead.csv overhead "$wh" 32 "${OVERHEAD_REPS:-5}" pg-tmpfs \
            --arms "$ARMS4,$nt" $(calarg)
    done
    "$PY" checks.py overhead results_server_overhead.csv || true
    ;;
  merge)
    # R15: what the merge is worth: unchopped with pa5+pa6 as one statement vs. two
    # (@nomerge), interleaved; a measurement, not a gate (E1/E2 carry the same side arms)
    arms="B-inc/unchopped,B-inc/unchopped@nomerge,B-rec/unchopped,B-rec/unchopped@nomerge"
    for c in 1 32; do
        cell results_server_merge.csv merge 1 "$c" "${MERGE_REPS:-$CAMPAIGN_REPS}" pg-tmpfs \
            --arms "$arms" $(calarg)
    done
    "$PY" checks.py merge results_server_merge.csv || true
    ;;
  locklog)
    # measured lock waits (log_lock_waits, deadlock_timeout = 1 ms) and what logging them costs
    for tag in off on; do
        svc=pg-tmpfs; [ "$tag" = on ] && svc=pg-tmpfs-locklog
        cell "results_server_locklog_${tag}.csv" "locklog-$tag" 1 32 "${LOCKLOG_REPS:-5}" \
            "$svc" --arms "B-inc/unchopped,B-inc/chopped" $(calarg)
    done
    "$PY" checks.py locklog results_server_locklog_off.csv results_server_locklog_on.csv || true
    ;;
  checks)
    for s in esmoke calibrate overhead merge locklog; do "$0" "$s"; done
    ;;
  drift)
    tag="${2:?usage: run_server.sh drift <start|mid|end>}"
    cell "results_server_drift_${tag}.csv" "drift-$tag" 1 32 "${DRIFT_REPS:-5}" pg-tmpfs \
        --arms "$ARMS4" $(calarg)
    "$PY" checks.py drift results_server_drift_*.csv || true
    ;;
  e1)
    for c in $(each "${E1_CLIENTS:-1,2,4,8,16,32,64}"); do
        cell results_server_e1.csv E1 1 "$c" "$CAMPAIGN_REPS" pg-tmpfs --arms "$ARMS6" $(calarg)
    done
    ;;
  e2)
    for w in $(each "${E2_WAREHOUSES:-2,4,8}"); do
        cell results_server_e2.csv E2 "$w" 32 "$CAMPAIGN_REPS" pg-tmpfs --arms "$ARMS6" $(calarg)
    done
    ;;
  e3)
    for c in $(each "${E3_CLIENTS:-8,32,64}"); do
        cell results_server_e3.csv E3 1 "$c" "${E3_REPS:-$CAMPAIGN_REPS}" pg-tmpfs --contiguous \
            $(calarg)
    done
    ;;
  campaign)
    [ -f calibration.json ] || { echo "run the checks first (no calibration.json)" >&2; exit 1; }
    "$0" drift start
    "$0" e1
    "$0" e2
    "$0" drift mid
    "$0" e3
    "$0" drift end
    "$0" figures
    ;;
  figures)
    # the paper's figures from this folder's results_server_*.csv, into figures_paper/
    "$PY" paper_figs.py . --out figures_paper
    "$PY" plot_fig12_predictors.py . --minmax --out figures_paper
    "$PY" checks.py drift results_server_drift_*.csv || true
    "$PY" summarize.py results_server_e1.csv results_server_e2.csv results_server_e3.csv
    ;;
  *)
    sed -n '2,11p' "$0"; exit 2
    ;;
esac

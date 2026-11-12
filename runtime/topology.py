"""
Proposes the server's core split from the CPU topology (Linux, `lscpu -p`): Postgres and the
client processes each get both hardware threads of whole physical cores, so they never share a
core through hyperthreading, and the core of CPU 0 is left to the OS (interrupts, Docker, the
sampler). Writes server.env, which run_server.sh reads; check it before the campaign.

  python3 topology.py                   # print the topology and the proposal, write server.env
  python3 topology.py --client-cores 6  # more client cores (default: about a fifth, at least 2)
"""
import argparse
import subprocess
from collections import defaultdict


def ranges(cpus):
    """[0,1,2,5] -> '0-2,5'"""
    cpus, out, i = sorted(cpus), [], 0
    while i < len(cpus):
        j = i
        while j + 1 < len(cpus) and cpus[j + 1] == cpus[j] + 1:
            j += 1
        out.append(str(cpus[i]) if i == j else f"{cpus[i]}-{cpus[j]}")
        i = j + 1
    return ",".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-cores", type=int, default=0)
    ap.add_argument("--out", default="server.env")
    args = ap.parse_args()
    text = subprocess.check_output(["lscpu", "-p=CPU,CORE,SOCKET,NODE"], text=True)
    cores = defaultdict(list)                 # (socket, core) -> hardware threads
    node = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        cpu, core, sock, nd = (int(x) if x else 0 for x in line.split(","))
        cores[(sock, core)].append(cpu)
        node[(sock, core)] = nd
    order = sorted(cores, key=lambda c: min(cores[c]))
    print(f"{len(order)} physical cores, {sum(len(v) for v in cores.values())} threads, "
          f"NUMA nodes {sorted(set(node.values()))}")
    for c in order:
        print(f"  socket {c[0]} core {c[1]:3d} node {node[c]}: threads {cores[c]}")
    os_core = next(c for c in order if 0 in cores[c])
    rest = [c for c in order if c != os_core]
    n_client = args.client_cores or max(2, round(len(rest) / 5))
    # clients on the last cores (on a two-socket machine: the second socket's end), Postgres on
    # the rest, so Postgres's cores stay contiguous from the first socket on
    client, pg = rest[-n_client:], rest[:-n_client]
    pg_cpus = [t for c in pg for t in cores[c]]
    client_cpus = [t for c in client for t in cores[c]]
    procs = len(client_cpus)
    env = (f"PG_CPUSET={ranges(pg_cpus)}\nCLIENT_CPUS={ranges(client_cpus)}\nPROCS={procs}\n")
    print(f"\nOS: core with threads {cores[os_core]}")
    print(f"Postgres: {len(pg)} cores, threads {ranges(pg_cpus)} "
          f"(NUMA nodes {sorted({node[c] for c in pg})})")
    print(f"clients:  {len(client)} cores, threads {ranges(client_cpus)}, {procs} processes")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(env)
    print(f"\nwrote {args.out}:\n{env}")


if __name__ == "__main__":
    main()

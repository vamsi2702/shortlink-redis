"""The concurrency experiment: asyncio against thread-per-connection, at 10, 100 and
1,000 simultaneous connections, with real Redis as the reference.

    python -m bench.concurrency                  # needs Docker running

Each server runs in its own container with one CPU. The load comes from `redis-benchmark` (a C
program, so the load generator cannot be the bottleneck), run inside the Redis container on the same
Docker network. For every server and connection count it records throughput, latency, and the CPU
time and memory the server used, read from the container's cgroup counters (exact, unlike sampling
`docker stats` during a few-second test).
"""

import argparse
import csv
import io
import json
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass

from bench import chart
from bench.run_benchmarks import RESULTS, ROOT, log

BASE = [
    "docker",
    "compose",
    "-f",
    "docker-compose.yml",
    "-f",
    "docker-compose.bench.yml",
    "--profile",
    "reference",
    "--profile",
    "tools",
]


@dataclass(frozen=True)
class Server:
    label: str
    service: str  # the compose service, which is also its network name
    port: int


SERVERS = {
    "redis": Server("Real Redis 7 (C, event loop)", "redis", 6379),
    "asyncio": Server("MiniRedis, asyncio (one thread)", "miniredis", 6380),
    "threaded": Server("MiniRedis, thread per connection", "miniredis-threaded", 6381),
}


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*BASE, *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=check,
    )


def container(service: str) -> str:
    """The container id of a running compose service."""
    return compose("ps", "-q", service).stdout.strip()


def read_cgroup(service: str, *paths: str) -> str:
    """The contents of the first of these cgroup files that exists in the container. Docker
    Desktop may use the older cgroup v1 layout or the newer v2, which name the files differently."""
    for path in paths:
        result = compose("exec", "-T", service, "cat", path, check=False)
        if result.returncode == 0:
            return result.stdout
    raise SystemExit(f"none of {paths} exists in the {service} container")


def cpu_microseconds(service: str) -> int:
    """CPU time the service's container has used so far, from its cgroup."""
    text = read_cgroup(service, "/sys/fs/cgroup/cpuacct/cpuacct.usage", "/sys/fs/cgroup/cpu.stat")
    if "usage_usec" in text:  # cgroup v2: a table of counters, in microseconds
        return int(re.search(r"usage_usec (\d+)", text).group(1))  # type: ignore[union-attr]
    return int(text.strip()) // 1000  # cgroup v1: one number, in nanoseconds


def memory_mib(service: str) -> float:
    text = read_cgroup(
        service, "/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory.current"
    )
    return int(text.strip()) / 2**20


def run_benchmark(server: Server, connections: int, requests: int, test: str) -> dict:
    """One redis-benchmark run against one server; returns its numbers, or {"error": ...}."""
    cpu_before, memory_before = cpu_microseconds(server.service), memory_mib(server.service)
    started = time.perf_counter()
    result = compose(
        "exec",
        "-T",
        "redis",
        "redis-benchmark",
        "-h",
        server.service,
        "-p",
        str(server.port),
        "-t",
        test,
        "-n",
        str(requests),
        "-c",
        str(connections),
        "--csv",
        check=False,
    )
    seconds = time.perf_counter() - started
    rows = list(csv.reader(io.StringIO(result.stdout)))
    # header: "test","rps","avg_latency_ms","min_latency_ms","p50_latency_ms","p95_latency_ms",...
    if result.returncode != 0 or len(rows) < 2:
        return {"error": (result.stderr or result.stdout).strip()[-300:] or "no output"}
    # one row per test (SET, then GET); the GET row is the headline, since a cache is read-heavy
    # and GET finds the keys that SET just wrote
    by_test = {row[0].upper(): dict(zip(rows[0], row, strict=False)) for row in rows[1:]}
    numbers = by_test.get("GET") or next(iter(by_test.values()))
    cpu_seconds = (cpu_microseconds(server.service) - cpu_before) / 1e6
    operations = requests * len(by_test)  # every test sends `requests` commands
    memory_after = memory_mib(server.service)
    return {
        "requests_per_s": float(numbers["rps"]),
        "p50_ms": float(numbers["p50_latency_ms"]),
        "p99_ms": float(numbers["p99_latency_ms"]),
        "max_ms": float(numbers["max_latency_ms"]),
        "set_requests_per_s": float(by_test["SET"]["rps"]) if "SET" in by_test else None,
        "cpu_seconds_per_100k_requests": cpu_seconds * 100_000 / operations,
        "memory_mib_after": memory_after,
        "memory_mib_added": memory_after - memory_before,
        "wall_seconds": seconds,
    }


def prepare() -> None:
    """Fresh servers, so the earlier connection counts do not leave anything behind."""
    compose(
        "up", "-d", "-V", "--force-recreate", "--wait", "redis", "miniredis", "miniredis-threaded"
    )
    compose("exec", "-T", "redis", "redis-cli", "config", "set", "save", "")


def median_of(runs: list[dict]) -> dict:
    """The median of each number over the runs, with the runs themselves kept. If any run
    failed, the cell is reported as failed (a server that sometimes breaks is not "fine")."""
    failed = [run for run in runs if "error" in run]
    if failed:
        return {"error": failed[0]["error"], "runs": runs}
    keys = [key for key, value in runs[0].items() if isinstance(value, int | float)]
    median = {
        key: statistics.median(run[key] for run in runs if run.get(key) is not None) for key in keys
    }
    return median | {"runs": runs}


def main() -> int:
    parser = argparse.ArgumentParser(description="asyncio vs threaded MiniRedis")
    parser.add_argument("--connections", default="10,100,1000")
    parser.add_argument("--requests", type=int, default=100_000)
    parser.add_argument("--repeats", type=int, default=3, help="runs per cell; the median is kept")
    parser.add_argument("--test", default="set,get", help="redis-benchmark -t value")
    parser.add_argument("--servers", default=",".join(SERVERS))
    parser.add_argument("--out", default="concurrency")
    options = parser.parse_args()
    levels = [int(value) for value in options.connections.split(",")]
    chosen = [name for name in options.servers.split(",")]

    log("building the image (it must contain miniredis/threaded_server.py)")
    compose("build", "api")
    results: dict = {"params": vars(options) | {"levels": levels}, "servers": {}}
    for name in chosen:
        server = SERVERS[name]
        results["servers"][name] = {"label": server.label, "levels": {}}
        for connections in levels:
            runs = []
            for number in range(1, options.repeats + 1):
                prepare()  # every run starts from a fresh server
                log(f"{name}: {connections} connections, run {number}/{options.repeats}")
                runs.append(run_benchmark(server, connections, options.requests, options.test))
            data = median_of(runs)
            results["servers"][name]["levels"][str(connections)] = data
            if "error" in data:
                log(f"    failed: {data['error']}")
            else:
                log(
                    f"    {data['requests_per_s']:,.0f} req/s, p50 {data['p50_ms']} ms, "
                    f"p99 {data['p99_ms']} ms, "
                    f"cpu {data['cpu_seconds_per_100k_requests']:.2f} s/100k, "
                    f"memory {data['memory_mib_added']:+.0f} MiB"
                )
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{options.out}.json").write_text(json.dumps(results, indent=2))
    (RESULTS / f"{options.out}.md").write_text(table(results) + "\n")
    chart.write_concurrency_chart(results, RESULTS / f"{options.out}.svg")
    print("\n" + table(results))
    return 0


def table(results: dict) -> str:
    """GET is the headline (a cache is read-heavy); the SET rate is shown beside it."""
    lines = [
        "| Server | Connections | GET req/s | GET p50 ms | GET p99 ms | SET req/s "
        "| CPU s per 100k commands | Memory added MiB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for data in results["servers"].values():
        for connections, run in data["levels"].items():
            if "error" in run:
                failed = f"failed: {run['error'][:60]}"
                lines.append(f"| {data['label']} | {connections} | {failed} | | | | | |")
                continue
            set_rate = run.get("set_requests_per_s")
            lines.append(
                f"| {data['label']} | {connections} | {run['requests_per_s']:,.0f} "
                f"| {run['p50_ms']} | {run['p99_ms']} "
                f"| {'n/a' if set_rate is None else f'{set_rate:,.0f}'} "
                f"| {run['cpu_seconds_per_100k_requests']:.2f} | {run['memory_mib_added']:.0f} |"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())

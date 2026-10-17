"""Run the redirect benchmark for each setup and write the results (needs Docker running).

    python -m bench.run_benchmarks                       # the four cache setups, ~10 minutes
    python -m bench.run_benchmarks --setups redis-full,miniredis-full

For each setup it follows the same recipe, so the comparison is fair:

  1. recreate both cache servers empty, so nothing carries over from the previous setup
  2. stop the API, reload the identical dataset (bench.seed), start the API configured for the setup
  3. a COLD run (no warm-up), then a warm-up of a fixed number of requests, then 3 measured runs

The load is sent by a container on the same Docker network as the API (docker-compose.bench.yml),
so the numbers do not include Windows' slow forwarding of ports into Docker.
"""

import argparse
import http.client
import itertools
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from bench import dataset, seed

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "bench" / "results"


@dataclass(frozen=True)
class Setup:
    label: str
    cache_url: str  # "" for none
    cache_enabled: bool = True
    rate_limit: bool = False  # the cache setups switch the limiter and the click queue off, so that
    clicks: bool = False  # only the cache is measured; the "-full" setups switch them on


SETUPS = {
    "none": Setup("No cache (every redirect asks Postgres)", "", cache_enabled=False),
    "inprocess": Setup("In-process dict (the original cache)", ""),
    "redis": Setup("Real Redis 7", "redis://redis:6379"),
    "miniredis": Setup("MiniRedis", "redis://miniredis:6380"),
    "redis-full": Setup(
        "Real Redis, rate limit and clicks on", "redis://redis:6379", True, True, True
    ),
    "miniredis-full": Setup(
        "MiniRedis, rate limit and clicks on", "redis://miniredis:6380", True, True, True
    ),
}
DEFAULT_SETUPS = ["none", "inprocess", "redis", "miniredis"]


def compose_command(env_file: Path, *args: str) -> list[str]:
    return [
        "docker", "compose", "--env-file", str(env_file),
        "-f", "docker-compose.yml", "-f", "docker-compose.bench.yml",
        "--profile", "reference", "--profile", "tools",
        *args,
    ]  # fmt: skip


def run(command: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    # utf-8 with "replace": Docker's progress output has bytes Windows' codepage cannot read
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        capture_output=capture,
    )


def write_env_file(setup: Setup, directory: Path) -> Path:
    path = directory / "bench.env"
    path.write_text(
        f"BENCH_CACHE_URL={setup.cache_url}\n"
        f"BENCH_CACHE_ENABLED={'true' if setup.cache_enabled else 'false'}\n"
        f"BENCH_RATE_LIMIT={'true' if setup.rate_limit else 'false'}\n"
        f"BENCH_CLICKS={'true' if setup.clicks else 'false'}\n"
    )
    return path


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def prepare(setup: Setup, env_file: Path, links: int) -> None:
    """Get to a known starting state: empty caches, the identical dataset, the API configured."""

    def compose(*args: str, capture: bool = False):
        return run(compose_command(env_file, *args), capture)

    compose("stop", "api", "worker")
    # -V also renews the anonymous volumes, so real Redis cannot reload a snapshot of earlier data
    compose("up", "-d", "-V", "--force-recreate", "--wait", "redis", "miniredis")
    compose(
        "exec", "-T", "redis", "redis-cli", "config", "set", "save", ""
    )  # no snapshots, as MiniRedis
    log(f"loading the dataset ({links:,} links)")
    seed.seed_database(seed.BENCH_DATABASE, links, dataset.DEFAULT_USERS, dataset.DEFAULT_SEED)
    services = ["api", "worker"] if setup.clicks else ["api"]
    compose("up", "-d", "--force-recreate", "--wait", *services)
    connection = http.client.HTTPConnection("localhost", 8000, timeout=10)
    connection.request("GET", f"/{dataset.make_codes(1)[0]}")
    status = connection.getresponse().status
    if status != 302:
        raise SystemExit(f"sanity check failed: GET /<first code> answered {status}, not 302")


def load(env_file: Path, args: list[str]) -> dict:
    """One run of the load generator container; returns its JSON result."""
    command = compose_command(
        env_file,
        "run",
        "--rm",
        "--no-deps",
        "loadgen",
        "--url",
        "http://api:8000",
        *args,
        "--json",
        "-",
    )
    output = run(command, capture=True).stdout.strip().splitlines()
    return json.loads(output[-1])


def summarize(runs: list[dict]) -> dict:
    """The median of each number over the runs (errors are added up)."""

    def median(key: str):
        values = [r[key] for r in runs if r[key] is not None]
        return statistics.median(values) if values else None

    return {
        "requests_per_s": median("requests_per_s"),
        "p50_ms": median("p50_ms"),
        "p95_ms": median("p95_ms"),
        "p99_ms": median("p99_ms"),
        "cache_hit_ratio": median("cache_hit_ratio"),
        "errors": sum(r["errors"] for r in runs),
        "requests": sum(r["requests"] for r in runs),
    }


def measure(setup: Setup, env_file: Path, options: argparse.Namespace) -> dict:
    """A cold run, then a fixed warm-up, then `repeats` measured runs at each load level."""
    common = ["--pattern", options.pattern]
    # Every load run gets its OWN seed, so it draws new requests from the same distribution.
    # With one shared seed, later runs would replay the first run's requests, find them all
    # cached, and report a hit ratio of 100% that real traffic would never reach.
    seeds = itertools.count(1)
    busiest = max(options.levels)
    log(f"cold run ({busiest} clients, cache empty, no warm-up)")
    cold = load(
        env_file,
        [
            *common,
            "--seed",
            str(next(seeds)),
            "--clients",
            str(busiest),
            "--duration",
            str(options.cold_seconds),
            "--warmup",
            "0",
        ],
    )
    levels = {}
    warmed_up = False
    for clients in sorted(options.levels, reverse=True):  # the busiest level warms the cache up
        runs = []
        for number in range(1, options.repeats + 1):
            # one fixed warm-up in total; every other run only settles for a moment
            warm = (
                ["--warmup", "2"]
                if warmed_up
                else ["--warmup-requests", str(options.warmup_requests)]
            )
            warmed_up = True
            log(f"measured run {number}/{options.repeats} at {clients} clients")
            runs.append(
                load(
                    env_file,
                    [
                        *common,
                        "--seed",
                        str(next(seeds)),
                        "--clients",
                        str(clients),
                        "--duration",
                        str(options.duration),
                        *warm,
                    ],
                )
            )
        levels[str(clients)] = {"runs": runs, "median": summarize(runs)}
    return {"label": setup.label, "cold": cold, "levels": levels}


def capture_environment(env_file: Path) -> dict:
    def ask(*args: str) -> str:
        try:
            return run(compose_command(env_file, *args), capture=True).stdout.strip()
        except subprocess.CalledProcessError:
            return "unknown"

    docker = run(
        ["docker", "info", "--format", "{{.ServerVersion}} cpus={{.NCPU}} memory={{.MemTotal}}"],
        True,
    )
    return {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "host": f"{platform.system()} {platform.release()}, {platform.processor()}",
        "docker": docker.stdout.strip(),
        "redis": ask("exec", "-T", "redis", "redis-server", "--version"),
        "python_in_containers": ask("exec", "-T", "api", "python", "--version"),
        "postgres": ask("exec", "-T", "postgres", "postgres", "--version"),
        "cpu_limits": "api 2, redis 1, miniredis 1, worker 1, load generator 4; postgres unlimited",
    }


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def table(results: dict) -> str:
    """One table per load level (the median of the measured runs), then the cold-start run."""
    blocks = []
    for clients in sorted(results["params"]["levels"]):
        lines = [
            f"**{clients} client(s)**",
            "",
            "| Setup | req/s | p50 ms | p95 ms | p99 ms | hit ratio | errors |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for data in results["setups"].values():
            m = data["levels"][str(clients)]["median"]
            lines.append(
                f"| {data['label']} | {m['requests_per_s']:,.0f} | {m['p50_ms']:.2f} "
                f"| {m['p95_ms']:.2f} | {m['p99_ms']:.2f} | {percent(m['cache_hit_ratio'])} "
                f"| {m['errors']} |"
            )
        blocks.append("\n".join(lines))
    cold_lines = [
        "**Cold start** (cache empty, no warm-up, first seconds under load)",
        "",
        "| Setup | req/s | p50 ms | p99 ms | hit ratio |",
        "|---|---:|---:|---:|---:|",
    ]
    for data in results["setups"].values():
        c = data["cold"]
        cold_lines.append(
            f"| {data['label']} | {c['requests_per_s']:,.0f} | {c['p50_ms']:.2f} "
            f"| {c['p99_ms']:.2f} | {percent(c['cache_hit_ratio'])} |"
        )
    return "\n\n".join([*blocks, "\n".join(cold_lines)])


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the redirect benchmark")
    parser.add_argument(
        "--setups", default=",".join(DEFAULT_SETUPS), help=f"from {', '.join(SETUPS)}"
    )
    parser.add_argument("--pattern", default="zipf", choices=["zipf", "uniform"])
    parser.add_argument(
        "--levels", default="1,32", help="numbers of simultaneous clients to measure at"
    )
    parser.add_argument("--duration", type=float, default=15.0, help="seconds per measured run")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup-requests", type=int, default=30_000)
    parser.add_argument("--cold-seconds", type=float, default=15.0)
    parser.add_argument("--links", type=int, default=dataset.DEFAULT_LINKS)
    parser.add_argument(
        "--out", default="redirects", help="name of the files written to bench/results"
    )
    options = parser.parse_args()
    options.levels = sorted({int(level) for level in options.levels.split(",")})

    chosen = [name.strip() for name in options.setups.split(",")]
    unknown = [name for name in chosen if name not in SETUPS]
    if unknown:
        parser.error(f"unknown setups {unknown}; choose from {list(SETUPS)}")

    RESULTS.mkdir(exist_ok=True)
    results: dict = {"params": vars(options), "setups": {}}
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        env_file = write_env_file(SETUPS[chosen[0]], directory)
        log("building the API image (it must contain the current code)")
        run(compose_command(env_file, "build", "api"), capture=True)
        for name in chosen:
            setup = SETUPS[name]
            log(f"=== {name}: {setup.label}")
            env_file = write_env_file(setup, directory)
            prepare(setup, env_file, options.links)
            results["setups"][name] = measure(setup, env_file, options)
            for clients, data in results["setups"][name]["levels"].items():
                median = data["median"]
                log(
                    f"    {clients:>3} clients: {median['requests_per_s']:,.0f} req/s, "
                    f"p50 {median['p50_ms']:.1f} ms, p99 {median['p99_ms']:.1f} ms, "
                    f"errors {median['errors']}"
                )
        results["environment"] = capture_environment(env_file)
        run(compose_command(env_file, "stop", "api", "worker"))

    (RESULTS / f"{options.out}.json").write_text(json.dumps(results, indent=2))
    (RESULTS / f"{options.out}.md").write_text(table(results) + "\n")
    print("\n" + table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Send a workload to the API from many clients and report latency and throughput.

    python -m bench.loadtest --url http://localhost:8000 --clients 50 --duration 20 --warmup 5

Every client sends one request, waits for the answer, then sends the next ("closed loop"). A warm-up
period runs first and is not measured, so the cache can fill. Then requests started during the
measured window are timed. Because a client waits for its answer, a slow server is offered less
load than real traffic would give it, so the tail latencies are a little kinder than in real life.
"""

import argparse
import asyncio
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field

import httpx

from bench import dataset
from bench.workload import DEFAULT_WRITE_RATIO, PATTERNS, Workload

EXPECTED_STATUS = {"read": 302, "write": 201}


@dataclass
class Result:
    pattern: str
    clients: int
    seconds: float  # length of the measured window
    requests: int
    errors: int  # exceptions, timeouts, and any status other than the expected one
    requests_per_s: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    cache_hit_ratio: float | None  # taken from the API's /metrics; None if it could not be read
    statuses: dict[str, int] = field(default_factory=dict)


def percentile(sorted_values: list[float], percent: float) -> float:
    """The value below which `percent` % of the (sorted) values fall (nearest-rank method)."""
    if not sorted_values:
        return 0.0
    rank = max(1, -(-len(sorted_values) * percent // 100))  # ceiling without importing math
    return sorted_values[int(rank) - 1]


def counter_value(metrics_text: str, name: str, **labels: str) -> float:
    """The value of the counter series `name{labels}` in Prometheus text (0 if absent)."""
    wanted = [f'{key}="{value}"' for key, value in labels.items()]
    for line in metrics_text.splitlines():
        if line.startswith(f"{name}{{") and all(part in line for part in wanted):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


async def cache_counts(client: httpx.AsyncClient) -> tuple[float, float] | None:
    """(hits, misses) so far, from the API's /metrics."""
    try:
        text = (await client.get("/metrics")).text
    except httpx.HTTPError:
        return None
    return (
        counter_value(text, "shortlink_cache_lookups_total", result="hit"),
        counter_value(text, "shortlink_cache_lookups_total", result="miss"),
    )


async def one_client(
    client: httpx.AsyncClient, workload: Workload, measure_from: float, end: float
) -> tuple[list[float], Counter[str], int]:
    """Send requests until `end`. Returns the timed latencies, counts per status, and errors."""
    latencies: list[float] = []
    statuses: Counter[str] = Counter()
    errors = 0
    while (started := time.perf_counter()) < end:
        request = workload.next()
        try:
            if request.kind == "read":
                response = await client.get(f"/{request.code}", follow_redirects=False)
            else:
                response = await client.post("/shorten", json={"url": request.url})
            status, failed = str(response.status_code), (
                response.status_code != EXPECTED_STATUS[request.kind]
            )
        except httpx.HTTPError as exc:
            status, failed = type(exc).__name__, True
        elapsed = time.perf_counter() - started
        if started >= measure_from:
            statuses[status] += 1
            latencies.append(elapsed)
            errors += failed
    return latencies, statuses, errors


async def run_load(
    base_url: str,
    codes: list[str],
    pattern: str = "zipf",
    clients: int = 10,
    duration: float = 10.0,
    warmup: float = 3.0,
    seed: int = 0,
    write_ratio: float = DEFAULT_WRITE_RATIO,
    timeout: float = 10.0,
) -> Result:
    limits = httpx.Limits(max_connections=clients + 2, max_keepalive_connections=clients + 2)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=timeout) as http:
        start = time.perf_counter()
        measure_from, end = start + warmup, start + warmup + duration
        workloads = [Workload(codes, pattern, seed * 1000 + i, write_ratio) for i in range(clients)]
        tasks = [asyncio.create_task(one_client(http, w, measure_from, end)) for w in workloads]

        await asyncio.sleep(max(0.0, measure_from - time.perf_counter()))
        before = await cache_counts(http)
        outputs = await asyncio.gather(*tasks)
        after = await cache_counts(http)

    latencies = sorted(value for lat, _, _ in outputs for value in lat)
    statuses: Counter[str] = Counter()
    for _, counts, _ in outputs:
        statuses.update(counts)
    hit_ratio = None
    if before and after:
        hits, misses = after[0] - before[0], after[1] - before[1]
        hit_ratio = hits / (hits + misses) if hits + misses else None
    return Result(
        pattern=pattern,
        clients=clients,
        seconds=duration,
        requests=len(latencies),
        errors=sum(errors for _, _, errors in outputs),
        requests_per_s=len(latencies) / duration,
        p50_ms=percentile(latencies, 50) * 1000,
        p95_ms=percentile(latencies, 95) * 1000,
        p99_ms=percentile(latencies, 99) * 1000,
        max_ms=(latencies[-1] if latencies else 0.0) * 1000,
        cache_hit_ratio=hit_ratio,
        statuses=dict(statuses),
    )


def format_result(result: Result) -> str:
    hit = "n/a" if result.cache_hit_ratio is None else f"{result.cache_hit_ratio:.1%}"
    return (
        f"{result.pattern}, {result.clients} clients, {result.seconds:g}s measured\n"
        f"  requests {result.requests:,}   errors {result.errors}   "
        f"{result.requests_per_s:,.0f} req/s\n"
        f"  latency ms: p50 {result.p50_ms:.2f}   p95 {result.p95_ms:.2f}   "
        f"p99 {result.p99_ms:.2f}   max {result.max_ms:.1f}\n"
        f"  cache hit ratio {hit}   statuses {result.statuses}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Load test the API")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--pattern", choices=PATTERNS, default="zipf")
    parser.add_argument("--clients", type=int, default=1)
    parser.add_argument("--duration", type=float, default=10.0, help="measured seconds")
    parser.add_argument("--warmup", type=float, default=3.0, help="seconds before measuring")
    parser.add_argument("--links", type=int, default=dataset.DEFAULT_LINKS)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--write-ratio", type=float, default=DEFAULT_WRITE_RATIO)
    parser.add_argument("--json", help="also write the result to this file")
    args = parser.parse_args()

    codes = dataset.make_codes(args.links)
    result = asyncio.run(
        run_load(
            args.url, codes, args.pattern, args.clients, args.duration, args.warmup, args.seed,
            args.write_ratio,
        )
    )
    print(format_result(result))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as out:
            json.dump(asdict(result), out, indent=2)


if __name__ == "__main__":
    main()

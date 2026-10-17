"""Send a workload to the API from many clients and report latency and throughput.

    python -m bench.loadtest --url http://localhost:8000 --clients 50 --duration 20 \
        --warmup-requests 20000

Every client sends one request, waits for the answer, then sends the next ("closed loop"). A warm-up
runs first and is not measured, so the cache can fill; it ends after a fixed NUMBER of requests, so
every setup does the same amount of warm-up work however fast it is. Then requests started during
the measured window are timed. Because a client waits for its answer, a slow server is offered
less load than real traffic would give it, so the tail latencies are a little kinder than real life.

The HTTP is written by hand on asyncio streams (class Connection) instead of using httpx. Measured
from inside Docker, httpx's async client topped out near 125 requests/s against the same server
that a plain asyncio client drove at about 500, so with httpx the load generator itself would have
been the bottleneck and the benchmark would have measured the tool, not the API.
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from bench import dataset
from bench.workload import DEFAULT_WRITE_RATIO, PATTERNS, Workload

EXPECTED_STATUS = {"read": 302, "write": 201}
HEADER_END = b"\r\n\r\n"
WINDOW_MARKER = "WINDOW-STARTED"
Counts = tuple[float, float] | None  # (cache hits, misses) read from /metrics, or None


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
    # one entry per second of the measured window, only when a timeline was asked for
    timeline: list[dict] | None = None


@dataclass
class Window:
    """Shared by the clients and the coordinator: counts requests, says when to measure and stop."""

    sent: int = 0
    start: float | None = None  # measuring begins here (None while warming up)
    end: float = float("inf")  # clients stop sending at this time
    # for a timeline: (seconds after the window opened, how long it took, whether it failed) for
    # every measured request; None when no timeline was asked for
    samples: list[tuple[float, float, bool]] | None = None


class RequestFailed(Exception):
    """A request got no usable answer (refused, reset, timed out, or garbled)."""


class Connection:
    """One keep-alive HTTP/1.1 connection to the API, with just enough HTTP for this project's
    answers: a status line, a Content-Length, and a body."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self._host, self._port, self._timeout = host, port, timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        """Returns (status, body). Raises RequestFailed; the next call then reconnects."""
        try:
            return await asyncio.wait_for(self._exchange(method, path, body), self._timeout)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
            self.close()
            raise RequestFailed(type(exc).__name__) from exc

    async def _exchange(self, method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
        if self._writer is None:
            self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        assert self._reader is not None
        head = f"{method} {path} HTTP/1.1\r\nHost: {self._host}\r\n"
        if body is not None:
            head += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        self._writer.write(head.encode() + b"\r\n" + (body or b""))
        answer = await self._reader.readuntil(HEADER_END)
        status = int(answer[9:12])
        length = 0
        for line in answer.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value)
            elif name.strip().lower() == b"transfer-encoding":
                raise ValueError("chunked answers are not supported")
        data = await self._reader.readexactly(length) if length else b""
        if b"connection: close" in answer.lower():
            self.close()
        return status, data

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = self._writer = None


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


async def cache_counts(connection: Connection) -> tuple[float, float] | None:
    """(hits, misses) so far, from the API's /metrics."""
    for attempt in range(3):
        try:
            _, body = await connection.request("GET", "/metrics")
            break
        except RequestFailed as exc:
            print(
                f"warning: reading /metrics failed ({exc}), attempt {attempt + 1}", file=sys.stderr
            )
    else:
        return None
    text = body.decode()
    return (
        counter_value(text, "shortlink_cache_lookups_total", result="hit"),
        counter_value(text, "shortlink_cache_lookups_total", result="miss"),
    )


async def one_client(
    connection: Connection, workload: Workload, window: Window
) -> tuple[list[float], Counter[str], int]:
    """Send requests until the window ends. Returns the timed latencies, status counts, errors."""
    latencies: list[float] = []
    statuses: Counter[str] = Counter()
    errors = 0
    while (started := time.perf_counter()) < window.end:
        request = workload.next()
        window.sent += 1
        try:
            if request.kind == "read":
                code, _ = await connection.request("GET", f"/{request.code}")
            else:
                payload = json.dumps({"url": request.url}).encode()
                code, _ = await connection.request("POST", "/shorten", payload)
            status, failed = str(code), code != EXPECTED_STATUS[request.kind]
        except RequestFailed as exc:
            status, failed = str(exc), True
            await asyncio.sleep(0.005)  # do not spin when the server is gone
        elapsed = time.perf_counter() - started
        if window.start is not None and started >= window.start:
            statuses[status] += 1
            latencies.append(elapsed)
            errors += failed
            if window.samples is not None:
                window.samples.append((started - window.start, elapsed, failed))
    return latencies, statuses, errors


async def coordinate(
    monitor: Connection,
    window: Window,
    duration: float,
    warmup_requests: int,
    warmup_seconds: float,
    timeline: bool = False,
) -> tuple[Counts, Counts, list[Counts]]:
    """Wait out the warm-up, open the measured window, and read the cache counters at both ends.

    With `timeline`, also read them at the end of every second (the third value returned) and
    print WINDOW-STARTED to stderr the moment measuring begins, for a script that is watching."""
    began = time.perf_counter()
    while (
        window.sent < warmup_requests
        if warmup_requests
        else time.perf_counter() - began < warmup_seconds
    ):
        await asyncio.sleep(0.02)
    before = await cache_counts(monitor)
    window.start = time.perf_counter()
    window.end = window.start + duration
    per_second: list[Counts] = [before]
    if timeline:
        window.samples = []
        print(WINDOW_MARKER, file=sys.stderr, flush=True)
        for second in range(1, int(duration) + 1):
            await asyncio.sleep(max(0.0, window.start + second - time.perf_counter()))
            per_second.append(await cache_counts(monitor))
    await asyncio.sleep(max(0.0, window.end + 0.1 - time.perf_counter()))  # let the last finish
    after = await cache_counts(monitor)
    return before, after, per_second


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
    warmup_requests: int = 0,
    timeline: bool = False,
) -> Result:
    """Run one load test. The warm-up is `warmup_requests` requests if that is given, otherwise
    `warmup` seconds. With `timeline`, the result also has a per-second breakdown."""
    url = urlsplit(base_url)
    host, port = url.hostname or "localhost", url.port or 80
    window = Window()
    workloads = [Workload(codes, pattern, seed * 1000 + i, write_ratio) for i in range(clients)]
    connections = [Connection(host, port, timeout) for _ in range(clients)]
    monitor = Connection(host, port, timeout)  # reads /metrics, apart from the clients
    tasks = [
        asyncio.create_task(one_client(connection, workload, window))
        for connection, workload in zip(connections, workloads, strict=True)
    ]
    before, after, per_second = await coordinate(
        monitor, window, duration, warmup_requests, warmup, timeline
    )
    outputs = await asyncio.gather(*tasks)
    for connection in (*connections, monitor):
        connection.close()

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
        timeline=build_timeline(window.samples, per_second) if timeline else None,
    )


def build_timeline(
    samples: list[tuple[float, float, bool]] | None, per_second: list[Counts]
) -> list[dict]:
    """Group the measured requests into seconds. For each second: how many requests started in it,
    how many failed, their median and 99th percentile latency, and the cache hits and misses the
    API counted during it (the difference between two readings of /metrics)."""
    seconds: dict[int, list[tuple[float, bool]]] = {}
    for offset, elapsed, failed in samples or []:
        seconds.setdefault(int(offset), []).append((elapsed, failed))
    timeline = []
    for second in range(len(per_second) - 1):
        entries = seconds.get(second, [])
        latencies = sorted(elapsed for elapsed, _ in entries)
        start, end = per_second[second], per_second[second + 1]
        timeline.append(
            {
                "t": second,
                "requests": len(entries),
                "errors": sum(failed for _, failed in entries),
                "p50_ms": percentile(latencies, 50) * 1000,
                "p99_ms": percentile(latencies, 99) * 1000,
                "hits": end[0] - start[0] if start and end else 0,
                "misses": end[1] - start[1] if start and end else 0,
            }
        )
    return timeline


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
    parser.add_argument("--warmup", type=float, default=3.0, help="warm-up seconds")
    parser.add_argument("--warmup-requests", type=int, default=0, help="warm up this many requests")
    parser.add_argument("--links", type=int, default=dataset.DEFAULT_LINKS)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--write-ratio", type=float, default=DEFAULT_WRITE_RATIO)
    parser.add_argument("--timeline", action="store_true", help="also report every second")
    parser.add_argument("--json", help="write the result to this file; '-' prints only the JSON")
    args = parser.parse_args()

    result = asyncio.run(
        run_load(
            args.url,
            dataset.make_codes(args.links),
            args.pattern,
            args.clients,
            args.duration,
            args.warmup,
            args.seed,
            args.write_ratio,
            warmup_requests=args.warmup_requests,
            timeline=args.timeline,
        )
    )
    if args.json == "-":
        print(json.dumps(asdict(result)))
        return
    print(format_result(result))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as out:
            json.dump(asdict(result), out, indent=2)


if __name__ == "__main__":
    main()

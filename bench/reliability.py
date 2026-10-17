"""The reliability test (step 5.4): kill MiniRedis in the middle of a load test.

    python -m bench.reliability                  # needs Docker; about 12 minutes for 3 repeats

For each repeat: reload the dataset, warm the cache, then run a load test of D seconds. At `kill`
seconds MiniRedis is killed with SIGKILL (a crash, not a tidy shutdown), and at `restart` seconds it
is started again, empty. It measures:

  * errors and latency before, during and after the outage (the API should keep serving redirects)
  * how long the cache takes to get back to its normal hit ratio after MiniRedis comes back empty
  * click accounting: redirects served, events queued, events dropped (MiniRedis was down),
    events saved to Postgres, and so events lost (queued but never saved: they died with MiniRedis)
"""

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

from bench import chart
from bench.loadtest import counter_value
from bench.run_benchmarks import (
    RESULTS,
    ROOT,
    SETUPS,
    compose_command,
    log,
    percent,
    prepare,
    run,
    write_env_file,
)

SETUP = SETUPS["miniredis-full"]  # rate limiter and click queue on, like the real system
WINDOW_MARKER = "WINDOW-STARTED"


def metrics_text() -> str:
    with urllib.request.urlopen("http://localhost:8000/metrics", timeout=10) as response:
        return response.read().decode()


def click_counts(env_file) -> dict:
    """The click bookkeeping now: API counters, rows in Postgres, events waiting in the queue."""
    text = metrics_text()
    rows = run(
        compose_command(
            env_file,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "shortlink",
            "-d",
            "shortlink_bench",
            "-t",
            "-A",
            "-c",
            "select count(*) from clicks",
        ),
        capture=True,
    ).stdout
    queue = run(
        compose_command(
            env_file,
            "exec",
            "-T",
            "miniredis",
            "python",
            "-c",
            "import socket;s=socket.create_connection(('127.0.0.1',6380));"
            "s.sendall(b'LLEN clicks\\r\\n');print(int(s.recv(64)[1:-2]))",
        ),
        capture=True,
    ).stdout
    return {
        "queued": counter_value(text, "shortlink_clicks_queued_total", result="queued"),
        "dropped": counter_value(text, "shortlink_clicks_queued_total", result="dropped"),
        "rows": int(rows.strip()),
        "queue_length": int(queue.strip()),
    }


def wait_until_drained(env_file, patience: float = 30.0) -> dict:
    """Wait until the worker has emptied the queue and the number of rows stops changing."""
    deadline = time.monotonic() + patience
    previous = None
    while time.monotonic() < deadline:
        counts = click_counts(env_file)
        if counts["queue_length"] == 0 and previous is not None and counts["rows"] == previous:
            return counts
        previous = counts["rows"]
        time.sleep(2)
    return click_counts(env_file)


def one_repeat(env_file, options: argparse.Namespace) -> dict:
    prepare(SETUP, env_file, options.links)
    command = compose_command(
        env_file,
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "loadgen",
        "--url",
        "http://api:8000",
        "--clients",
        str(options.clients),
        "--duration",
        str(options.duration),
        "--warmup-requests",
        str(options.warmup_requests),
        "--seed",
        "1",
        "--timeline",
        "--json",
        "-",
    )
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    events: dict = {}
    before: dict = {}
    result_line = ""

    def disturb(started: float) -> None:
        """Kill MiniRedis, then bring it back, at the planned moments after the window opened."""
        time.sleep(max(0.0, started + options.kill - time.monotonic()))
        run(compose_command(env_file, "kill", "miniredis"))
        events["killed_at"] = time.monotonic() - started
        time.sleep(max(0.0, started + options.restart - time.monotonic()))
        run(compose_command(env_file, "start", "miniredis"))
        events["restarted_at"] = time.monotonic() - started

    assert process.stdout is not None
    for line in process.stdout:
        if WINDOW_MARKER in line:
            started = time.monotonic()
            before = click_counts(env_file)
            threading.Thread(target=disturb, args=(started,), daemon=True).start()
            log(f"    measuring; MiniRedis dies at {options.kill}s, returns at {options.restart}s")
        elif line.startswith("{"):
            result_line = line
    process.wait()
    if not result_line or not before:
        raise SystemExit("the load test did not produce a result")
    load = json.loads(result_line)
    after = wait_until_drained(env_file)
    return analyse(load, before, after, events, options)


def window(timeline: list[dict], first: float, last: float) -> list[dict]:
    return [s for s in timeline if first <= s["t"] < last]


def hit_ratio(seconds: list[dict]) -> float | None:
    hits, misses = sum(s["hits"] for s in seconds), sum(s["misses"] for s in seconds)
    return hits / (hits + misses) if hits + misses else None


def latency(seconds: list[dict], key: str) -> float | None:
    values = [s[key] for s in seconds if s["requests"]]
    return statistics.median(values) if values else None


def recovery_seconds(
    timeline: list[dict], restarted: float, baseline: float, fraction: float
) -> float | None:
    """Seconds after the restart until 3 seconds in a row reach `fraction` of the normal hit
    ratio, or None if that never happened before the test ended."""
    for second in sorted(s["t"] for s in timeline if s["t"] >= restarted):
        ratio = hit_ratio(window(timeline, second, second + 3))
        if ratio is not None and ratio >= fraction * baseline:
            return second - restarted
    return None


def analyse(load: dict, before: dict, after: dict, events: dict, options) -> dict:
    timeline = load["timeline"]
    killed, restarted = events["killed_at"], events["restarted_at"]
    normal = window(timeline, 5, killed)  # skip the first seconds: connections still settling
    outage = window(timeline, killed + 2, restarted)
    recovered = window(timeline, options.duration - 20, options.duration)  # the last 20 seconds
    baseline = hit_ratio(normal) or 0.0

    served = load["statuses"].get("302", 0)
    queued = after["queued"] - before["queued"]
    dropped = after["dropped"] - before["dropped"]
    # events queued before the window opened but saved during it must not count as saved here
    saved = (after["rows"] - before["rows"]) - before["queue_length"]
    return {
        "errors_total": load["errors"],
        "errors_during_outage": sum(s["errors"] for s in window(timeline, killed, restarted + 5)),
        "requests": load["requests"],
        "p50_ms": {
            "before": latency(normal, "p50_ms"),
            "during": latency(outage, "p50_ms"),
            "after": latency(recovered, "p50_ms"),
        },
        "p99_ms": {
            "before": latency(normal, "p99_ms"),
            "during": latency(outage, "p99_ms"),
            "after": latency(recovered, "p99_ms"),
        },
        "requests_per_s": {
            "before": statistics.mean([s["requests"] for s in normal]) if normal else None,
            "during": statistics.mean([s["requests"] for s in outage]) if outage else None,
        },
        "hit_ratio": {
            "before": baseline,
            "during": hit_ratio(outage),
            "after": hit_ratio(recovered),
        },
        "warmup_seconds_after_restart": {
            f"{round(100 * fraction)}%": recovery_seconds(timeline, restarted, baseline, fraction)
            for fraction in (0.5, 0.75, 0.9)
        },
        "clicks": {
            "redirects_served": served,
            "queued": queued,
            "dropped_while_down": dropped,
            "saved": saved,
            "lost_in_queue_when_killed": queued - saved,
            "lost_total": served - saved,
            "lost_percent": 100 * (served - saved) / served if served else None,
        },
        "kill_at": killed,
        "restart_at": restarted,
        "timeline": timeline,
    }


def summary_table(repeats: list[dict]) -> str:
    def cell(value, spec=".1f"):
        return "n/a" if value is None else format(value, spec)

    def warmup_cell(seconds: dict) -> str:  # "n/a": not reached before the test ended
        return " / ".join(cell(seconds[key], ".0f") for key in ("50%", "75%", "90%"))

    lines = [
        "| Repeat | errors | p50 ms before / during / after | hit ratio before / during / after "
        "| seconds to 50% / 75% / 90% of the normal hit ratio | clicks served "
        "| dropped (MiniRedis down) "
        "| lost in queue at kill | lost % |",
        "|---|---:|---|---|---|---:|---:|---:|---:|",
    ]
    for number, r in enumerate(repeats, 1):
        p, h, c = r["p50_ms"], r["hit_ratio"], r["clicks"]
        lines.append(
            f"| {number} | {r['errors_total']} "
            f"| {cell(p['before'])} / {cell(p['during'])} / {cell(p['after'])} "
            f"| {percent(h['before'])} / {percent(h['during'])} / {percent(h['after'])} "
            f"| {warmup_cell(r['warmup_seconds_after_restart'])} | {c['redirects_served']:,} "
            f"| {c['dropped_while_down']:,} | {c['lost_in_queue_when_killed']:,} "
            f"| {cell(c['lost_percent'])}% |"
        )
    return "\n".join(lines)


def write_chart(repeat: dict, path) -> None:
    """Latency and hit ratio second by second, with the outage marked."""
    seconds = repeat["timeline"]
    markers = [(repeat["kill_at"], "MiniRedis killed"), (repeat["restart_at"], "restarted, empty")]
    ratio = [
        (s["t"], 100 * s["hits"] / (s["hits"] + s["misses"]))
        for s in seconds
        if s["hits"] + s["misses"]
    ]
    latency_p50 = [(s["t"], s["p50_ms"]) for s in seconds if s["requests"]]
    rate = [(s["t"], s["requests"]) for s in seconds]
    chart.document(
        [
            chart.line_chart(
                "Cache hit ratio",
                {"hit ratio": ratio},
                "seconds",
                "percent",
                markers=markers,
                unit="%",
            ),
            chart.line_chart(
                "Median latency",
                {"p50": latency_p50},
                "seconds",
                "milliseconds",
                markers=markers,
                unit=" ms",
            ),
            chart.line_chart(
                "Redirects served per second",
                {"req/s": rate},
                "seconds",
                "requests/s",
                markers=markers,
            ),
        ],
        path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Kill MiniRedis under load")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--kill", type=float, default=15.0, help="seconds into the test")
    parser.add_argument("--restart", type=float, default=30.0)
    parser.add_argument("--warmup-requests", type=int, default=20_000)
    parser.add_argument("--links", type=int, default=100_000)
    parser.add_argument("--out", default="reliability")
    options = parser.parse_args()

    import tempfile
    from pathlib import Path

    RESULTS.mkdir(exist_ok=True)
    repeats = []
    with tempfile.TemporaryDirectory() as temp:
        env_file = write_env_file(SETUP, Path(temp))
        log("building the API image (it must contain the current code)")
        run(compose_command(env_file, "build", "api"), capture=True)
        for number in range(1, options.repeats + 1):
            log(f"=== repeat {number}/{options.repeats}")
            repeats.append(one_repeat(env_file, options))
            c = repeats[-1]["clicks"]
            log(
                f"    errors {repeats[-1]['errors_total']}, warm-up to 50/75/90% "
                f"{repeats[-1]['warmup_seconds_after_restart']} s, "
                f"clicks served {c['redirects_served']:,}, "
                f"lost {c['lost_total']:,} ({c['lost_percent']:.2f}%)"
            )
        run(compose_command(env_file, "stop", "api", "worker"))
    results = {"params": vars(options), "repeats": repeats}
    (RESULTS / f"{options.out}.json").write_text(json.dumps(results, indent=2))
    (RESULTS / f"{options.out}.md").write_text(summary_table(repeats) + "\n")
    write_chart(repeats[0], RESULTS / f"{options.out}.svg")
    print("\n" + summary_table(repeats))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Numbers about the running API, in the Prometheus text format (served at /metrics).

Counters only ever go up (requests handled, cache hits, ...). The latency histogram counts how many
requests took no longer than each of a few limits, which is enough to work out percentiles later.

The numbers live in this process's memory and start again from zero when it restarts.
"""

import threading
from bisect import bisect_left

# Request durations in seconds: a request is counted in every bucket whose limit it did not exceed
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)

REQUESTS = "shortlink_http_requests_total"
LATENCY = "shortlink_http_request_duration_seconds"
CACHE_LOOKUPS = "shortlink_cache_lookups_total"
RATE_LIMITED = "shortlink_rate_limited_total"
CLICKS_QUEUED = "shortlink_clicks_queued_total"
MINIREDIS_ERRORS = "shortlink_miniredis_errors_total"

HELP = {
    REQUESTS: "HTTP requests handled, by route template, method and status",
    LATENCY: "Time to handle an HTTP request, in seconds",
    CACHE_LOOKUPS: "Redirect cache lookups, by result (hit or miss)",
    RATE_LIMITED: "Requests refused with 429, by rate limit scope",
    CLICKS_QUEUED: "Click events for the worker, by result (queued or dropped)",
    MINIREDIS_ERRORS: "Failed calls to MiniRedis, by kind",
}

Labels = tuple[tuple[str, str], ...]  # sorted (name, value) pairs; usable as a dict key


def _labels(values: dict[str, str]) -> Labels:
    return tuple(sorted(values.items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format(name: str, labels: Labels, value: float) -> str:
    if labels:
        inside = ",".join(f'{key}="{_escape(text)}"' for key, text in labels)
        return f"{name}{{{inside}}} {value:g}"
    return f"{name} {value:g}"


class _Histogram:
    def __init__(self) -> None:
        self.buckets = [0] * (len(LATENCY_BUCKETS) + 1)  # the last one is "more than all limits"
        self.total = 0.0
        self.count = 0


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()  # FastAPI handles requests in several threads
        self._counters: dict[str, dict[Labels, float]] = {}
        self._histograms: dict[str, dict[Labels, _Histogram]] = {}

    def inc(self, name: str, amount: float = 1, **labels: str) -> None:
        with self._lock:
            series = self._counters.setdefault(name, {})
            key = _labels(labels)
            series[key] = series.get(key, 0) + amount

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        with self._lock:
            histogram = self._histograms.setdefault(name, {}).setdefault(
                _labels(labels), _Histogram()
            )
            histogram.buckets[bisect_left(LATENCY_BUCKETS, seconds)] += 1
            histogram.total += seconds
            histogram.count += 1

    def value(self, name: str, **labels: str) -> float:
        """The current value of one counter series (0 if it has not been counted yet)."""
        with self._lock:
            return self._counters.get(name, {}).get(_labels(labels), 0)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()

    def render(self) -> str:
        """All the numbers in the Prometheus text format."""
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._counters):
                lines += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} counter"]
                for labels, value in sorted(self._counters[name].items()):
                    lines.append(_format(name, labels, value))
            for name in sorted(self._histograms):
                lines += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} histogram"]
                for labels, histogram in sorted(self._histograms[name].items()):
                    running = 0
                    for limit, in_bucket in zip(LATENCY_BUCKETS, histogram.buckets, strict=False):
                        running += in_bucket  # each bucket counts everything up to its limit
                        le = (*labels, ("le", f"{limit:g}"))
                        lines.append(_format(f"{name}_bucket", le, running))
                    inf = (*labels, ("le", "+Inf"))
                    lines.append(_format(f"{name}_bucket", inf, histogram.count))
                    lines.append(_format(f"{name}_sum", labels, histogram.total))
                    lines.append(_format(f"{name}_count", labels, histogram.count))
        return "\n".join(lines) + "\n" if lines else ""


# The one registry for this process
metrics = Metrics()

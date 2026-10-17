"""The requests a load test sends: which link to visit next, and how often to create one.

    python -m bench.workload --pattern zipf --count 20    # a few requests, and how skewed they are

Two access patterns:

  zipf     the link of popularity rank k is visited with probability proportional to 1 / k**s. A few
           links get most of the traffic, as on the real web, which is what makes a cache useful.
  uniform  every link is equally likely. The worst case for a cache.

About 95% of requests are redirects (reads) and about 5% create a link (writes).
"""

import argparse
import random
from bisect import bisect_left
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import accumulate, islice

from bench import dataset

PATTERNS = ("zipf", "uniform")
DEFAULT_ZIPF_S = 1.0
DEFAULT_WRITE_RATIO = 0.05


@dataclass(frozen=True)
class Request:
    kind: str  # "read" (GET /<code>) or "write" (POST /shorten)
    code: str | None = None  # for a read
    url: str | None = None  # for a write


class Workload:
    """An endless, repeatable stream of requests over a list of existing codes."""

    def __init__(
        self,
        codes: list[str],
        pattern: str = "zipf",
        seed: int = 0,
        write_ratio: float = DEFAULT_WRITE_RATIO,
        zipf_s: float = DEFAULT_ZIPF_S,
    ) -> None:
        if pattern not in PATTERNS:
            raise ValueError(f"pattern must be one of {PATTERNS}, not {pattern!r}")
        if not codes:
            raise ValueError("there are no codes to visit")
        if not 0 <= write_ratio <= 1:
            raise ValueError("write_ratio must be between 0 and 1")
        self._codes = codes
        self._pattern = pattern
        self._seed = seed
        self._write_ratio = write_ratio
        # cumulative weights of ranks 1..n; a random number in [0, total) lands on a rank, and the
        # first ranks have the biggest weights, so they are hit the most
        self._cumulative = (
            list(accumulate(1 / rank**zipf_s for rank in range(1, len(codes) + 1)))
            if pattern == "zipf"
            else []
        )
        self._rng = random.Random(seed)
        self._writes = 0

    def next(self) -> Request:
        if self._rng.random() < self._write_ratio:
            self._writes += 1
            return Request("write", url=f"https://example.com/bench/{self._seed}/{self._writes}")
        return Request("read", code=self._codes[self._pick()])

    def _pick(self) -> int:
        if self._pattern == "uniform":
            return int(self._rng.random() * len(self._codes))
        point = self._rng.random() * self._cumulative[-1]
        return min(bisect_left(self._cumulative, point), len(self._codes) - 1)

    def stream(self) -> Iterator[Request]:
        while True:
            yield self.next()


def hot_share(requests: list[Request], codes: list[str], fraction: float = 0.01) -> float:
    """The share of the reads that went to the most popular `fraction` of the links."""
    hot = set(codes[: max(1, int(len(codes) * fraction))])
    reads = [r for r in requests if r.kind == "read"]
    return sum(r.code in hot for r in reads) / len(reads) if reads else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Show the requests a load test would send")
    parser.add_argument("--pattern", choices=PATTERNS, default="zipf")
    parser.add_argument("--count", type=int, default=20, help="requests to show")
    parser.add_argument("--links", type=int, default=dataset.DEFAULT_LINKS)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    codes = dataset.make_codes(args.links)
    workload = Workload(codes, args.pattern, args.seed)
    for request in islice(workload.stream(), args.count):
        print(request)
    sample = list(islice(Workload(codes, args.pattern, args.seed).stream(), 100_000))
    print(f"\n{args.pattern}: the hottest 1% of {args.links:,} links get "
          f"{hot_share(sample, codes):.0%} of 100,000 sampled reads")


if __name__ == "__main__":
    main()

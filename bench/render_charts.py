"""Redraw every chart in bench/results from the saved JSON results (no benchmark is run).

python -m bench.render_charts
"""

import json

from bench import chart
from bench.reliability import write_chart as write_reliability_chart
from bench.run_benchmarks import RESULTS


def main() -> None:
    redirects = RESULTS / "redirects.json"
    if redirects.exists():
        chart.write_benchmark_chart(json.loads(redirects.read_text()), RESULTS / "redirects.svg")
        print("redirects.svg")
    concurrency = RESULTS / "concurrency.json"
    if concurrency.exists():
        chart.write_concurrency_chart(
            json.loads(concurrency.read_text()), RESULTS / "concurrency.svg"
        )
        print("concurrency.svg")
    for name in ("reliability", "reliability-before-fix"):
        source = RESULTS / f"{name}.json"
        if source.exists():  # the chart shows the first repeat
            repeats = json.loads(source.read_text())["repeats"]
            write_reliability_chart(repeats[0], RESULTS / f"{name}.svg")
            print(f"{name}.svg")


if __name__ == "__main__":
    main()

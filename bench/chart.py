"""Draw the benchmark results as SVG charts, with no plotting library.

Each chart function returns a Panel: an SVG fragment together with its own width and height, so
the size a chart was drawn at and the space `document` reserves for it can never disagree.
Series are told apart by colour AND by a direct label, so the colours are never the only clue.
"""

import math
from html import escape
from pathlib import Path
from typing import NamedTuple

PALETTE = ["#2563eb", "#ea580c", "#16a34a", "#9333ea", "#0891b2", "#be123c"]
TEXT, MUTED, GRID = "#1f2937", "#6b7280", "#e5e7eb"
FONT = "font-family='system-ui, -apple-system, Segoe UI, sans-serif'"
MARGIN = {"left": 64, "right": 24, "top": 40, "bottom": 52}


class Panel(NamedTuple):
    svg: str  # the drawing, positioned inside a box of `width` x `height`
    width: int
    height: int


def _number(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:.0f}"
    digits = 1 if abs(value) >= 10 else 2
    return f"{value:.{digits}f}".rstrip("0").rstrip(".") or "0"  # 10, 12.3, 1.5, 0.25


def _nice_max(value: float) -> float:
    """A round number at or above `value`, for the top of an axis."""
    if value <= 0:
        return 1.0
    step = 10 ** math.floor(math.log10(value))
    for multiple in (1, 2, 2.5, 5, 10):
        if multiple * step >= value:
            return multiple * step
    return 10 * step


def _nice_ticks(low: float, high: float, target: int = 6) -> list[float]:
    """Round numbers between `low` and `high` (20, 40, 60, ...) to put labels on."""
    if high <= low:
        return [low]
    wanted = (high - low) / target
    magnitude = 10 ** math.floor(math.log10(wanted))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= wanted)
    tick = math.ceil(low / step) * step
    ticks = []
    while tick <= high + step * 1e-9:
        ticks.append(round(tick, 10))
        tick += step
    return ticks


def _text(
    x: float,
    y: float,
    content: str,
    size: int = 12,
    anchor: str = "middle",
    fill: str = TEXT,
    weight: str = "normal",
    rotate: float = 0,
) -> str:
    transform = f" transform='rotate({rotate} {x} {y})'" if rotate else ""
    return (
        f"<text x='{x:.1f}' y='{y:.1f}' font-size='{size}' text-anchor='{anchor}' fill='{fill}' "
        f"font-weight='{weight}' {FONT}{transform}>{escape(content)}</text>"
    )


def _frame(title: str, ylabel: str, xlabel: str, width: int, height: int) -> list[str]:
    plot_height = height - MARGIN["top"] - MARGIN["bottom"]
    plot_width = width - MARGIN["left"] - MARGIN["right"]
    return [
        _text(width / 2, 22, title, 15, weight="600"),
        _text(16, MARGIN["top"] + plot_height / 2, ylabel, 11, fill=MUTED, rotate=-90),
        _text(MARGIN["left"] + plot_width / 2, height - 8, xlabel, 11, fill=MUTED),
    ]


def _y_axis(
    top_value: float, x0: float, x1: float, y0: float, y1: float, unit: str = ""
) -> list[str]:
    parts = []
    for tick in range(6):
        value = top_value * tick / 5
        y = y1 - (y1 - y0) * tick / 5
        parts.append(f"<line x1='{x0}' x2='{x1}' y1='{y:.1f}' y2='{y:.1f}' stroke='{GRID}'/>")
        parts.append(_text(x0 - 6, y + 4, _number(value) + unit, 11, "end", MUTED))
    return parts


def line_chart(
    title: str,
    series: dict[str, list[tuple[float, float]]],
    xlabel: str,
    ylabel: str,
    log_x: bool = False,
    markers: list[tuple[float, str]] | None = None,
    width: int = 640,
    height: int = 300,
    unit: str = "",
) -> Panel:
    """Lines through (x, y) points. `markers` are vertical lines with a label, e.g. an outage."""
    x0, x1 = MARGIN["left"], width - MARGIN["right"]
    y0, y1 = MARGIN["top"], height - MARGIN["bottom"]
    xs = [x for points in series.values() for x, _ in points]
    ys = [y for points in series.values() for _, y in points]
    top = _nice_max(max(ys, default=1))
    lo, hi = (math.log10(min(xs)), math.log10(max(xs))) if log_x else (min(xs), max(xs))
    span = (hi - lo) or 1

    def px(x: float) -> float:
        return x0 + (x1 - x0) * ((math.log10(x) if log_x else x) - lo) / span

    def py(y: float) -> float:
        return y1 - (y1 - y0) * y / top

    parts = _frame(title, ylabel, xlabel, width, height) + _y_axis(top, x0, x1, y0, y1, unit)
    ticks = sorted(set(xs)) if log_x else _nice_ticks(min(xs), max(xs))
    for x in ticks:
        parts.append(_text(px(x), y1 + 18, _number(x), 11, fill=MUTED))
    for index, (x, label) in enumerate(markers or []):
        parts.append(
            f"<line x1='{px(x):.1f}' x2='{px(x):.1f}' y1='{y0}' y2='{y1}' stroke='{MUTED}' "
            f"stroke-dasharray='4 3'/>"
        )
        near_right_edge = px(x) > x1 - 120
        parts.append(  # staggered, so two close markers do not write over each other
            _text(
                px(x) + (-4 if near_right_edge else 4),
                y0 + 14 + 15 * index,
                label,
                11,
                "end" if near_right_edge else "start",
                MUTED,
            )
        )
    for index, (name, points) in enumerate(series.items()):
        colour = PALETTE[index % len(PALETTE)]
        path = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in points)
        parts.append(
            f"<polyline points='{path}' fill='none' stroke='{colour}' stroke-width='2.5'/>"
        )
        for x, y in points if len(points) <= 12 else []:
            parts.append(f"<circle cx='{px(x):.1f}' cy='{py(y):.1f}' r='3.5' fill='{colour}'/>")
        if len(series) > 1:  # with one line, the title already says what it is
            last_x, last_y = points[-1]
            label_x = min(px(last_x), x1 - 4)
            parts.append(_text(label_x, py(last_y) - 8, name, 11, "end", colour, "600"))
    return Panel("".join(parts), width, height)


def bar_chart(
    title: str,
    groups: list[str],
    series: dict[str, list[float]],
    ylabel: str,
    width: int = 640,
    height: int = 340,
    unit: str = "",
) -> Panel:
    """Grouped bars: one cluster per group, one bar per series, each with its value on top."""
    x0, x1 = MARGIN["left"], width - MARGIN["right"]
    y0, y1 = MARGIN["top"], height - MARGIN["bottom"]
    top = _nice_max(max((v for values in series.values() for v in values), default=1))
    parts = _frame(title, ylabel, "", width, height) + _y_axis(top, x0, x1, y0, y1, unit)
    cluster = (x1 - x0) / len(groups)
    bar = cluster * 0.8 / len(series)
    for g, group in enumerate(groups):
        start = x0 + cluster * g + cluster * 0.1
        parts.append(_text(x0 + cluster * (g + 0.5), y1 + 18, group, 12, fill=TEXT))
        for s, values in enumerate(series.values()):
            height_px = (y1 - y0) * values[g] / top
            x = start + bar * s
            colour = PALETTE[s % len(PALETTE)]
            parts.append(
                f"<rect x='{x:.1f}' y='{y1 - height_px:.1f}' width='{bar - 3:.1f}' "
                f"height='{height_px:.1f}' fill='{colour}' rx='2'/>"
            )
            parts.append(_text(x + (bar - 3) / 2, y1 - height_px - 5, _number(values[g]), 11))
    for s, name in enumerate(series):  # legend
        parts.append(
            f"<rect x='{x0 + 150 * s}' y='{height - 22}' width='10' height='10' "
            f"fill='{PALETTE[s % len(PALETTE)]}' rx='2'/>"
        )
        parts.append(_text(x0 + 150 * s + 15, height - 13, name, 11, "start", MUTED))
    return Panel("".join(parts), width, height)


def document(panels: list[Panel], path: Path) -> None:
    """Stack panels vertically into one SVG file, each in exactly the space it was drawn for."""
    width = max(panel.width for panel in panels)
    height = sum(panel.height for panel in panels)
    body, offset = [], 0
    for panel in panels:
        body.append(f"<g transform='translate(0 {offset})'>{panel.svg}</g>")
        offset += panel.height
    path.write_text(
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' "
        f"width='{width}' height='{height}'>"
        f"<rect width='100%' height='100%' fill='white' rx='8'/>{''.join(body)}</svg>\n",
        encoding="utf-8",
    )


# ---- the project's own charts -------------------------------------------------------------------


def write_concurrency_chart(results: dict, path: Path) -> None:
    """Throughput and p99 latency against the number of connections, for each server."""
    rps, p99 = {}, {}
    for data in results["servers"].values():
        good = [(int(c), r) for c, r in data["levels"].items() if "error" not in r]
        if good:
            rps[data["label"]] = [(c, r["requests_per_s"]) for c, r in good]
            p99[data["label"]] = [(c, r["p99_ms"]) for c, r in good]
    document(
        [
            line_chart(
                "Throughput by connections",
                rps,
                "simultaneous connections",
                "requests/s",
                log_x=True,
            ),
            line_chart(
                "p99 latency by connections",
                p99,
                "simultaneous connections",
                "milliseconds",
                log_x=True,
                unit=" ms",
            ),
        ],
        path,
    )


def write_benchmark_chart(results: dict, path: Path) -> None:
    """Redirect throughput at the busiest level, and latency at one client, for each setup."""
    levels = sorted(int(c) for c in results["params"]["levels"])
    busy, single = str(levels[-1]), str(levels[0])
    names = list(results["setups"])
    labels = [results["setups"][n]["label"].split(" (")[0].split(",")[0] for n in names]
    throughput = [results["setups"][n]["levels"][busy]["median"]["requests_per_s"] for n in names]
    p50 = [results["setups"][n]["levels"][single]["median"]["p50_ms"] for n in names]
    p99 = [results["setups"][n]["levels"][single]["median"]["p99_ms"] for n in names]
    document(
        [
            bar_chart(
                f"Redirects per second at {busy} clients (higher is better)",
                labels,
                {"req/s": throughput},
                "requests/s",
            ),
            bar_chart(
                f"Latency with {single} client (lower is better)",
                labels,
                {"p50": p50, "p99": p99},
                "milliseconds",
                unit=" ms",
            ),
        ],
        path,
    )

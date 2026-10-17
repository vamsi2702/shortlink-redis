import xml.etree.ElementTree as ET

from bench import chart

NS = "{http://www.w3.org/2000/svg}"


def parse(fragment: str):
    """The fragment inside an <svg> wrapper, parsed (so it must be well-formed XML)."""
    return ET.fromstring(f"<svg xmlns='http://www.w3.org/2000/svg'>{fragment}</svg>")


def texts(root) -> list[str]:
    return [element.text for element in root.iter(f"{NS}text")]


def test_a_line_chart_has_one_line_per_series_and_labels_it():
    fragment = chart.line_chart(
        "Title",
        {"alpha": [(1, 10), (2, 20)], "beta": [(1, 5), (2, 30)]},
        "x axis",
        "y axis",
    )

    root = parse(fragment)

    assert len(list(root.iter(f"{NS}polyline"))) == 2
    assert {"Title", "alpha", "beta", "x axis", "y axis"} <= set(texts(root))


def test_a_line_chart_marks_each_point_when_there_are_few():
    root = parse(chart.line_chart("t", {"a": [(1, 1), (2, 2), (3, 3)]}, "x", "y"))

    assert len(list(root.iter(f"{NS}circle"))) == 3


def test_a_line_chart_with_many_points_has_no_dots():
    points = [(x, x) for x in range(60)]

    root = parse(chart.line_chart("t", {"a": points}, "x", "y"))

    assert not list(root.iter(f"{NS}circle"))


def test_vertical_markers_are_drawn_with_their_labels():
    root = parse(chart.line_chart("t", {"a": [(0, 1), (10, 2)]}, "x", "y", markers=[(4, "killed")]))

    assert "killed" in texts(root)
    assert any(line.get("stroke-dasharray") for line in root.iter(f"{NS}line"))


def test_a_log_axis_puts_the_ticks_at_the_data_points():
    root = parse(chart.line_chart("t", {"a": [(10, 1), (100, 2), (1000, 3)]}, "x", "y", log_x=True))

    assert {"10", "100", "1,000"} <= set(texts(root))


def test_the_points_of_a_log_axis_are_evenly_spaced_per_factor_of_ten():
    root = parse(chart.line_chart("t", {"a": [(10, 1), (100, 1), (1000, 1)]}, "x", "y", log_x=True))
    points = next(root.iter(f"{NS}polyline")).get("points").split()
    xs = [float(point.split(",")[0]) for point in points]

    assert abs((xs[1] - xs[0]) - (xs[2] - xs[1])) < 0.2


def test_a_bar_chart_has_a_bar_and_a_value_label_for_each_value():
    root = parse(chart.bar_chart("t", ["one", "two"], {"p50": [1.5, 3.0], "p99": [4.0, 9.0]}, "ms"))

    rects = list(root.iter(f"{NS}rect"))
    bars = [r for r in rects if float(r.get("height")) > 10]
    swatches = [r for r in rects if float(r.get("height")) == 10]  # the legend's colour squares
    assert len(bars) == 4
    assert len(swatches) == 2
    assert {"1.5", "3", "4", "9", "one", "two"} <= set(texts(root))


def test_bars_are_in_proportion():
    root = parse(chart.bar_chart("t", ["g"], {"a": [10.0], "b": [20.0]}, "y"))

    heights = sorted(
        float(r.get("height")) for r in root.iter(f"{NS}rect") if float(r.get("height")) > 11
    )
    assert abs(heights[1] / heights[0] - 2.0) < 0.01


def test_text_is_escaped():
    root = parse(chart.line_chart("a < b & c", {"x>y": [(1, 1), (2, 2)]}, "x", "y"))

    assert "a < b & c" in texts(root)  # it parsed, so the markup was escaped properly


def test_numbers_are_formatted_for_reading():
    assert chart._number(12345) == "12,345"
    assert chart._number(150.4) == "150"
    assert chart._number(12.34) == "12.3"
    assert chart._number(1.5) == "1.5"
    assert chart._number(0) == "0"


def test_axes_end_on_a_round_number():
    assert chart._nice_max(0.7) == 1
    assert chart._nice_max(13) == 20
    assert chart._nice_max(240) == 250
    assert chart._nice_max(0) == 1


def test_a_document_is_a_valid_svg_file(tmp_path):
    path = tmp_path / "out.svg"
    panel = chart.line_chart("t", {"a": [(1, 1), (2, 2)]}, "x", "y")

    chart.document([(panel, 640, 340), (panel, 640, 340)], path)

    root = ET.parse(path).getroot()
    assert root.tag == f"{NS}svg"
    assert root.get("viewBox") == "0 0 640 680"
    assert len(list(root.iter(f"{NS}g"))) == 2


def test_the_concurrency_chart_skips_failed_runs(tmp_path):
    results = {
        "servers": {
            "asyncio": {
                "label": "asyncio",
                "levels": {
                    "10": {"requests_per_s": 20000, "p99_ms": 1.0},
                    "1000": {"requests_per_s": 15000, "p99_ms": 9.0},
                },
            },
            "threaded": {
                "label": "threaded",
                "levels": {
                    "10": {"requests_per_s": 9000, "p99_ms": 2.0},
                    "1000": {"error": "could not start threads"},
                },
            },
        }
    }
    path = tmp_path / "concurrency.svg"

    chart.write_concurrency_chart(results, path)

    root = ET.parse(path).getroot()
    assert len(list(root.iter(f"{NS}polyline"))) == 4  # two servers, in each of two panels


def test_round_numbers_have_no_trailing_zeros():
    assert chart._number(10) == "10"
    assert chart._number(20.0) == "20"
    assert chart._number(0.5) == "0.5"
    assert chart._number(100) == "100"

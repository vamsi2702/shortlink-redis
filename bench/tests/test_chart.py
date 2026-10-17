import xml.etree.ElementTree as ET

from bench import chart

NS = "{http://www.w3.org/2000/svg}"


def parse(panel):
    """A panel's drawing inside an <svg> wrapper, parsed (so it must be well-formed XML)."""
    return ET.fromstring(f"<svg xmlns='http://www.w3.org/2000/svg'>{panel.svg}</svg>")


def texts(root) -> list[str]:
    return [element.text for element in root.iter(f"{NS}text")]


def test_a_line_chart_has_one_line_per_series_and_labels_it():
    panel = chart.line_chart(
        "Title",
        {"alpha": [(1, 10), (2, 20)], "beta": [(1, 5), (2, 30)]},
        "x axis",
        "y axis",
    )

    root = parse(panel)

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
    panel = chart.line_chart("t", {"a": [(1, 1), (2, 2)]}, "x", "y", height=300)

    chart.document([panel, panel], path)

    root = ET.parse(path).getroot()
    assert root.tag == f"{NS}svg"
    assert root.get("viewBox") == "0 0 640 600"
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


# ---- layout: nothing overlaps, nothing is drawn outside its panel --------------------------------


def y_values(root) -> list[float]:
    """Every vertical position drawn in a panel."""
    found = []
    for element in root.iter():
        for attribute in ("y", "y1", "y2", "cy"):
            if element.get(attribute) is not None:
                found.append(float(element.get(attribute)))
        if element.tag == f"{NS}polyline":
            found += [float(pair.split(",")[1]) for pair in element.get("points").split()]
    return found


def test_everything_is_drawn_inside_the_panel_it_belongs_to():
    """The bug this guards against: a chart drawn taller than the space it was given, so it ran
    into the chart below it."""
    for height in (260, 300, 340):
        root = parse(chart.line_chart("t", {"a": [(0, 1), (60, 9)]}, "seconds", "y", height=height))

        assert max(y_values(root)) <= height

    for height in (280, 340):
        root = parse(chart.bar_chart("t", ["a", "b"], {"x": [1.0, 2.0]}, "y", height=height))

        assert max(y_values(root)) <= height


def test_a_panel_says_how_big_it_is_and_the_document_gives_it_that_space(tmp_path):
    short = chart.line_chart("t", {"a": [(1, 1), (2, 2)]}, "x", "y", height=200)
    tall = chart.line_chart("t", {"a": [(1, 1), (2, 2)]}, "x", "y", height=350)
    path = tmp_path / "out.svg"

    chart.document([short, tall], path)

    root = ET.parse(path).getroot()
    offsets = [group.get("transform") for group in root.iter(f"{NS}g")]
    assert offsets == [
        "translate(0 0)",
        "translate(0 200)",
    ]  # the second starts where the first ends
    assert root.get("viewBox") == "0 0 640 550"


def test_a_time_axis_has_round_tick_marks():
    assert chart._nice_ticks(0, 119) == [0, 20, 40, 60, 80, 100]
    assert chart._nice_ticks(0, 60) == [0, 10, 20, 30, 40, 50, 60]
    assert chart._nice_ticks(5, 5) == [5]


def test_the_time_axis_labels_are_not_fractions():
    height = 300
    root = parse(chart.line_chart("t", {"a": [(0, 1), (119, 2)]}, "seconds", "y", height=height))
    tick_row = height - chart.MARGIN["bottom"] + 18  # where the x-axis tick labels are written

    ticks = [t.text for t in root.iter(f"{NS}text") if abs(float(t.get("y")) - tick_row) < 0.5]

    assert ticks == ["0", "20", "40", "60", "80", "100"]


def test_two_markers_close_together_do_not_write_over_each_other():
    root = parse(
        chart.line_chart(
            "t", {"a": [(0, 1), (120, 2)]}, "x", "y", markers=[(15, "killed"), (30, "restarted")]
        )
    )

    heights = {
        t.text: float(t.get("y"))
        for t in root.iter(f"{NS}text")
        if t.text in {"killed", "restarted"}
    }

    assert heights["killed"] != heights["restarted"]


def test_a_marker_near_the_right_edge_writes_to_its_left():
    root = parse(chart.line_chart("t", {"a": [(0, 1), (100, 2)]}, "x", "y", markers=[(98, "late")]))

    label = next(t for t in root.iter(f"{NS}text") if t.text == "late")

    assert label.get("text-anchor") == "end"


def test_a_single_line_has_no_label_of_its_own_but_several_do():
    one = parse(chart.line_chart("t", {"only": [(1, 1), (2, 2)]}, "x", "y"))
    two = parse(chart.line_chart("t", {"a": [(1, 1), (2, 2)], "b": [(1, 2), (2, 3)]}, "x", "y"))

    assert "only" not in texts(one)
    assert {"a", "b"} <= set(texts(two))

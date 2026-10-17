from bench.concurrency import median_of, table


def run(rps, p99=1.0, set_rps=None):
    return {
        "requests_per_s": rps,
        "p50_ms": 0.5,
        "p99_ms": p99,
        "set_requests_per_s": set_rps,
        "cpu_seconds_per_100k_requests": 2.0,
        "memory_mib_added": 1.0,
    }


def test_the_median_of_three_runs_is_the_middle_one():
    result = median_of([run(100), run(300), run(200)])

    assert result["requests_per_s"] == 200


def test_every_number_gets_its_own_median():
    result = median_of([run(100, p99=9.0), run(300, p99=1.0), run(200, p99=5.0)])

    assert (result["requests_per_s"], result["p99_ms"]) == (200, 5.0)  # different runs each


def test_the_runs_themselves_are_kept():
    runs = [run(1), run(2), run(3)]

    assert median_of(runs)["runs"] == runs


def test_a_missing_number_is_left_out_not_a_crash():
    result = median_of([run(1), run(2), run(3)])  # none of them has a SET rate

    assert "set_requests_per_s" not in result


def test_a_set_rate_is_kept_when_every_run_has_one():
    result = median_of([run(1, set_rps=10.0), run(2, set_rps=30.0), run(3, set_rps=20.0)])

    assert result["set_requests_per_s"] == 20.0


def test_one_failed_run_makes_the_whole_cell_failed():
    """A server that breaks even once is not "fine": do not average the failure away."""
    result = median_of([run(100), {"error": "connection reset"}, run(300)])

    assert result["error"] == "connection reset"
    assert len(result["runs"]) == 3


def test_the_table_shows_get_as_the_headline_and_set_beside_it():
    results = {
        "servers": {
            "asyncio": {
                "label": "MiniRedis, asyncio",
                "levels": {"10": median_of([run(20_000, set_rps=18_000)] * 3)},
            }
        }
    }

    text = table(results)

    assert "GET req/s" in text and "SET req/s" in text
    assert "| MiniRedis, asyncio | 10 | 20,000 | 0.5 | 1.0 | 18,000 | 2.00 | 1 |" in text


def test_a_failed_cell_is_shown_as_failed_and_the_table_still_renders():
    results = {
        "servers": {
            "threaded": {
                "label": "MiniRedis, threaded",
                "levels": {"1000": {"error": "could not start threads", "runs": []}},
            }
        }
    }

    text = table(results)

    assert "failed: could not start threads" in text

import re

import pytest
from fastapi.testclient import TestClient

from api import config
from api.deps import get_cache, get_click_queue
from api.main import app
from api.services import metrics as m
from api.services.metrics import Metrics, metrics


@pytest.fixture(autouse=True)
def fresh_metrics():
    metrics.reset()
    yield
    metrics.reset()


def scrape(client) -> str:
    return client.get("/metrics").text


def sample(client, name, **labels) -> float | None:
    """The value of one line of /metrics, or None if there is no such line."""
    inside = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
    pattern = re.escape(f"{name}{{{inside}}} " if labels else f"{name} ") + r"(\S+)"
    found = re.search(pattern, scrape(client))
    return float(found.group(1)) if found else None


# ---- the registry -------------------------------------------------------------------------------


def test_a_counter_counts_up_per_label_set():
    registry = Metrics()

    registry.inc("things_total", color="red")
    registry.inc("things_total", color="red")
    registry.inc("things_total", 5, color="blue")

    assert registry.value("things_total", color="red") == 2
    assert registry.value("things_total", color="blue") == 5
    assert registry.value("things_total", color="green") == 0


def test_the_text_format():
    registry = Metrics()
    registry.inc("things_total", color="red")
    registry.inc("plain_total")

    assert registry.render() == (
        "# HELP plain_total plain_total\n"
        "# TYPE plain_total counter\n"
        "plain_total 1\n"
        "# HELP things_total things_total\n"
        "# TYPE things_total counter\n"
        'things_total{color="red"} 1\n'
    )


def test_label_values_are_escaped():
    registry = Metrics()

    registry.inc("x_total", route='a"b\\c\nd')

    assert 'x_total{route="a\\"b\\\\c\\nd"} 1' in registry.render()


def test_nothing_recorded_renders_nothing():
    assert Metrics().render() == ""


def test_histogram_buckets_are_cumulative():
    registry = Metrics()
    for seconds in (0.003, 0.03, 0.03, 0.4, 10):
        registry.observe("latency", seconds, route="/x")

    text = registry.render()

    assert 'latency_bucket{route="/x",le="0.005"} 1' in text
    assert 'latency_bucket{route="/x",le="0.025"} 1' in text
    assert 'latency_bucket{route="/x",le="0.05"} 3' in text
    assert 'latency_bucket{route="/x",le="0.5"} 4' in text
    assert 'latency_bucket{route="/x",le="2.5"} 4' in text
    assert 'latency_bucket{route="/x",le="+Inf"} 5' in text
    assert 'latency_count{route="/x"} 5' in text
    assert 'latency_sum{route="/x"} 10.463' in text


def test_a_value_exactly_on_a_limit_counts_in_that_bucket():
    registry = Metrics()

    registry.observe("latency", 0.05)

    assert 'latency_bucket{le="0.05"} 1' in registry.render()
    assert 'latency_bucket{le="0.025"} 0' in registry.render()


# ---- the endpoint -------------------------------------------------------------------------------


def test_the_endpoint_serves_prometheus_text(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_request_counts_go_up_as_requests_are_made(client):
    for _ in range(3):
        client.get("/health")

    assert sample(client, m.REQUESTS, route="/health", method="GET", status="200") == 3
    client.get("/health")
    assert sample(client, m.REQUESTS, route="/health", method="GET", status="200") == 4


def test_requests_are_counted_by_route_template_not_by_the_real_path(client):
    codes = [client.post("/shorten", json={"url": f"https://example.com/{i}"}).json()["code"]
             for i in range(3)]
    for code in codes:
        client.get(f"/{code}", follow_redirects=False)

    text = scrape(client)

    assert sample(client, m.REQUESTS, route="/{code}", method="GET", status="302") == 3
    assert not any(code in text for code in codes)  # no series per short code
    assert sample(client, m.REQUESTS, route="/shorten", method="POST", status="201") == 3


def test_statuses_are_told_apart(client):
    client.get("/nosuchcode", follow_redirects=False)
    client.post("/shorten", json={"url": "not a url"})

    assert sample(client, m.REQUESTS, route="/{code}", method="GET", status="404") == 1
    assert sample(client, m.REQUESTS, route="/shorten", method="POST", status="422") == 1


def test_paths_that_match_no_route_are_counted_together(client):
    client.get("/a/b/c")
    client.get("/x/y")

    assert sample(client, m.REQUESTS, route="unmatched", method="GET", status="404") == 2


def test_a_crashing_request_is_counted_as_500(db):
    def broken_cache():
        raise RuntimeError("boom")

    app.dependency_overrides[get_cache] = broken_cache
    try:
        crashing = TestClient(app, raise_server_exceptions=False)
        assert crashing.get("/abc123").status_code == 500
    finally:
        app.dependency_overrides.clear()

    assert metrics.value(m.REQUESTS, route="/{code}", method="GET", status="500") == 1


def test_latency_is_recorded_for_every_request(client):
    for _ in range(4):
        client.get("/health")

    assert sample(client, m.LATENCY + "_count", route="/health") == 4
    assert f'{m.LATENCY}_bucket{{route="/health",le="+Inf"}} 4' in scrape(client)
    assert sample(client, m.LATENCY + "_sum", route="/health") > 0


def test_scraping_does_not_change_the_numbers(client):
    client.get("/health")

    before = scrape(client)
    after = scrape(client)

    assert before == after
    assert "/metrics" not in after


def test_metrics_are_not_rate_limited(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)

    assert [client.get("/metrics").status_code for _ in range(5)] == [200] * 5


# ---- what the app counts ------------------------------------------------------------------------


def test_cache_hits_and_misses(client):
    code = client.post("/shorten", json={"url": "https://example.com/c"}).json()["code"]

    client.get(f"/{code}", follow_redirects=False)  # not cached yet
    client.get(f"/{code}", follow_redirects=False)
    client.get(f"/{code}", follow_redirects=False)

    assert sample(client, m.CACHE_LOOKUPS, result="miss") == 1
    assert sample(client, m.CACHE_LOOKUPS, result="hit") == 2


def test_a_cached_not_found_counts_as_a_hit(client):
    client.get("/nosuchcode", follow_redirects=False)
    client.get("/nosuchcode", follow_redirects=False)

    assert sample(client, m.CACHE_LOOKUPS, result="miss") == 1
    assert sample(client, m.CACHE_LOOKUPS, result="hit") == 1


def test_rate_limited_requests_are_counted_by_scope(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    code = client.post("/shorten", json={"url": "https://example.com/r"}).json()["code"]

    for _ in range(4):
        client.get(f"/{code}", follow_redirects=False)

    assert sample(client, m.RATE_LIMITED, scope="redirect") == 3
    assert sample(client, m.RATE_LIMITED, scope="shorten") is None


def test_queued_and_dropped_clicks(client):
    code = client.post("/shorten", json={"url": "https://example.com/q"}).json()["code"]

    client.get(f"/{code}", follow_redirects=False)  # the default queue drops clicks
    assert sample(client, m.CLICKS_QUEUED, result="dropped") == 1

    class Accepting:
        def push(self, event):
            return True

    app.dependency_overrides[get_click_queue] = lambda: Accepting()
    client.get(f"/{code}", follow_redirects=False)
    assert sample(client, m.CLICKS_QUEUED, result="queued") == 1


def test_miniredis_failures_are_counted(client, redis_cache, miniredis_server):
    app.dependency_overrides[get_cache] = lambda: redis_cache
    code = client.post("/shorten", json={"url": "https://example.com/e"}).json()["code"]
    miniredis_server.stop()

    for _ in range(5):
        client.get(f"/{code}", follow_redirects=False)

    assert sample(client, m.MINIREDIS_ERRORS, kind="unreachable") >= 1
    assert sample(client, m.MINIREDIS_ERRORS, kind="skipped") >= 1  # the breaker spared the rest

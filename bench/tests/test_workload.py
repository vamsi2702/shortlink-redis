import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice

import pytest

from bench import dataset
from bench.loadtest import (
    WINDOW_MARKER,
    Connection,
    RequestFailed,
    build_timeline,
    counter_value,
    format_result,
    percentile,
    run_load,
)
from bench.workload import Request, Workload, hot_share

CODES = dataset.make_codes(10_000)


def sample(workload: Workload, count: int) -> list[Request]:
    return list(islice(workload.stream(), count))


# ---- the workload -------------------------------------------------------------------------------


def test_zipf_sends_most_reads_to_a_few_links():
    requests = sample(Workload(CODES, "zipf", seed=1), 30_000)

    # with s = 1, the hottest 1% of 10,000 links get about half of all reads
    assert hot_share(requests, CODES) > 0.45


def test_uniform_spreads_reads_evenly():
    requests = sample(Workload(CODES, "uniform", seed=1), 30_000)

    assert hot_share(requests, CODES) < 0.03  # about 1%, with some luck either way


def test_the_same_seed_gives_the_same_requests():
    first = sample(Workload(CODES, "zipf", seed=5), 500)
    second = sample(Workload(CODES, "zipf", seed=5), 500)

    assert first == second
    assert first != sample(Workload(CODES, "zipf", seed=6), 500)


def test_about_five_percent_of_requests_are_writes():
    requests = sample(Workload(CODES, "zipf", seed=2), 20_000)

    writes = sum(r.kind == "write" for r in requests)
    assert 0.04 < writes / len(requests) < 0.06


def test_the_write_ratio_can_be_changed():
    none = sample(Workload(CODES, seed=2, write_ratio=0.0), 1000)
    all_ = sample(Workload(CODES, seed=2, write_ratio=1.0), 1000)

    assert all(r.kind == "read" for r in none)
    assert all(r.kind == "write" for r in all_)


def test_reads_name_a_real_code_and_writes_a_fresh_url():
    codes = set(CODES)
    requests = sample(Workload(CODES, "zipf", seed=3), 2000)

    reads = [r for r in requests if r.kind == "read"]
    writes = [r for r in requests if r.kind == "write"]
    assert all(r.code in codes and r.url is None for r in reads)
    assert all(r.url.startswith("https://example.com/bench/") and r.code is None for r in writes)
    assert len({r.url for r in writes}) == len(writes)  # every created link is different


def test_clients_with_different_seeds_create_different_links():
    one = {r.url for r in sample(Workload(CODES, seed=1, write_ratio=1.0), 50)}
    two = {r.url for r in sample(Workload(CODES, seed=2, write_ratio=1.0), 50)}

    assert not one & two


def test_a_single_code_works():
    requests = sample(Workload(["abc123"], "zipf", seed=1, write_ratio=0.0), 20)

    assert {r.code for r in requests} == {"abc123"}


def test_bad_settings_are_refused():
    with pytest.raises(ValueError, match="pattern"):
        Workload(CODES, "weird")
    with pytest.raises(ValueError, match="no codes"):
        Workload([])
    with pytest.raises(ValueError, match="write_ratio"):
        Workload(CODES, write_ratio=2)


# ---- numbers ------------------------------------------------------------------------------------


def test_percentiles_use_the_nearest_rank():
    values = list(range(1, 101))  # 1 .. 100

    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 99) == 99
    assert percentile(values, 100) == 100
    assert percentile([7.0], 99) == 7.0
    assert percentile([], 50) == 0.0


def test_a_counter_is_read_from_prometheus_text():
    text = (
        "# TYPE shortlink_cache_lookups_total counter\n"
        'shortlink_cache_lookups_total{result="hit"} 90\n'
        'shortlink_cache_lookups_total{result="miss"} 10\n'
    )

    assert counter_value(text, "shortlink_cache_lookups_total", result="hit") == 90
    assert counter_value(text, "shortlink_cache_lookups_total", result="miss") == 10
    assert counter_value(text, "shortlink_cache_lookups_total", result="other") == 0
    assert counter_value("", "anything") == 0


# ---- the load tester, against a tiny fake server -------------------------------------------------


class FakeApi(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    redirect_status = 302
    hits = 0
    misses = 0
    clients: set = set()  # the client addresses seen: one per TCP connection
    close_next = False  # answer the next request with "Connection: close"

    def _reply(self, status, body=b"", headers=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        type(self).clients.add(self.client_address)
        if type(self).close_next:
            type(self).close_next = False
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/metrics":
            text = (
                f'shortlink_cache_lookups_total{{result="hit"}} {type(self).hits}\n'
                f'shortlink_cache_lookups_total{{result="miss"}} {type(self).misses}\n'
            )
            self._reply(200, text.encode())
        else:
            type(self).hits += 3  # pretend three of every four lookups were cached
            type(self).misses += 1
            self._reply(type(self).redirect_status, headers={"Location": "https://example.com/"})

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._reply(201, b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_api():
    FakeApi.redirect_status, FakeApi.hits, FakeApi.misses = 302, 0, 0
    FakeApi.clients, FakeApi.close_next = set(), False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def load(url, **options):
    options = {"clients": 3, "duration": 0.5, "warmup": 0.2, "seed": 1, **options}
    return asyncio.run(run_load(url, CODES, **options))


def test_a_load_test_counts_requests_and_reports_latency(fake_api):
    result = load(fake_api)

    assert result.requests > 20
    assert result.errors == 0
    assert result.requests_per_s == pytest.approx(result.requests / 0.5)
    assert 0 < result.p50_ms <= result.p95_ms <= result.p99_ms <= result.max_ms
    assert set(result.statuses) == {"302", "201"}  # reads and writes
    assert "req/s" in format_result(result)


def test_the_cache_hit_ratio_comes_from_the_servers_own_numbers(fake_api):
    result = load(fake_api, write_ratio=0.0)

    assert result.cache_hit_ratio == pytest.approx(0.75)


def test_unexpected_statuses_count_as_errors(fake_api):
    FakeApi.redirect_status = 500

    result = load(fake_api, write_ratio=0.0)

    assert result.errors == result.requests > 0
    assert result.statuses == {"500": result.requests}


def test_a_server_that_is_not_there_gives_errors_not_a_crash():
    result = load("http://127.0.0.1:1", timeout=0.5)

    assert result.requests > 0
    assert result.errors == result.requests
    assert result.cache_hit_ratio is None


def test_the_warmup_can_be_counted_in_requests(fake_api):
    seen = []
    real_get = FakeApi.do_GET

    def counting_get(self):
        seen.append(self.path)
        real_get(self)

    FakeApi.do_GET = counting_get
    try:
        result = load(fake_api, write_ratio=0.0, warmup_requests=40, warmup=99, duration=0.3)
    finally:
        FakeApi.do_GET = real_get

    redirects = [path for path in seen if path != "/metrics"]
    assert len(redirects) >= 40 + result.requests  # the warm-up requests were sent but not timed
    assert result.requests > 0


# ---- the hand-written HTTP connection -----------------------------------------------------------


def request(connection, method="GET", path="/abc", body=None):
    return asyncio.run(connection.request(method, path, body))


def test_one_connection_is_reused_for_many_requests(fake_api):
    FakeApi.clients = set()
    host, port = fake_api.removeprefix("http://").split(":")

    async def several():
        connection = Connection(host, int(port), timeout=5)
        statuses = [(await connection.request("GET", "/abc"))[0] for _ in range(5)]
        connection.close()
        return statuses

    assert asyncio.run(several()) == [302] * 5
    assert len(FakeApi.clients) == 1  # five requests, one TCP connection


def test_a_post_sends_its_body_and_a_get_reads_a_body(fake_api):
    host, port = fake_api.removeprefix("http://").split(":")

    async def both():
        connection = Connection(host, int(port), timeout=5)
        posted = await connection.request("POST", "/shorten", b'{"url": "https://example.com/"}')
        metrics = await connection.request("GET", "/metrics")
        connection.close()
        return posted, metrics

    posted, metrics = asyncio.run(both())

    assert posted == (201, b"{}")
    assert metrics[0] == 200 and b"shortlink_cache_lookups_total" in metrics[1]


def test_it_reconnects_after_the_server_hangs_up(fake_api):
    FakeApi.clients = set()
    FakeApi.close_next = True
    host, port = fake_api.removeprefix("http://").split(":")

    async def two_requests():
        connection = Connection(host, int(port), timeout=5)
        first = (await connection.request("GET", "/abc"))[0]  # answered, then the server closes
        second = (await connection.request("GET", "/abc"))[0]  # needs a new connection
        connection.close()
        return first, second

    assert asyncio.run(two_requests()) == (302, 302)
    assert len(FakeApi.clients) == 2


def test_a_connection_error_is_reported_and_the_next_call_tries_again():
    connection = Connection("127.0.0.1", 1, timeout=0.5)  # nothing listens on port 1

    # (Windows may time out where Linux would refuse at once, so the error's name is not checked)
    with pytest.raises(RequestFailed):
        request(connection)
    with pytest.raises(RequestFailed):
        request(connection)  # a second attempt is made (not stuck in a broken state)


# ---- the per-second timeline --------------------------------------------------------------------


def test_samples_are_grouped_into_seconds():
    samples = [
        (0.1, 0.010, False),
        (0.9, 0.030, False),
        (1.2, 0.020, True),  # a failed request in the second second
        (2.5, 0.040, False),
    ]
    readings = [(0.0, 0.0), (10.0, 5.0), (10.0, 5.0), (40.0, 5.0)]  # hits and misses at each second

    timeline = build_timeline(samples, readings)

    assert [s["t"] for s in timeline] == [0, 1, 2]
    assert [s["requests"] for s in timeline] == [2, 1, 1]
    assert [s["errors"] for s in timeline] == [0, 1, 0]
    assert timeline[0]["p50_ms"] == pytest.approx(10)  # nearest rank: the lower of two
    assert (timeline[0]["hits"], timeline[0]["misses"]) == (10, 5)
    assert (timeline[1]["hits"], timeline[1]["misses"]) == (0, 0)
    assert (timeline[2]["hits"], timeline[2]["misses"]) == (30, 0)


def test_a_second_with_no_requests_is_still_listed():
    timeline = build_timeline([(0.5, 0.01, False)], [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)])

    assert [s["requests"] for s in timeline] == [1, 0]
    assert timeline[1]["p50_ms"] == 0 and timeline[1]["p99_ms"] == 0


def test_a_missing_metrics_reading_gives_zero_hits_not_a_crash():
    timeline = build_timeline([(0.5, 0.01, False)], [None, (5.0, 5.0)])

    assert (timeline[0]["hits"], timeline[0]["misses"]) == (0, 0)


def test_a_timeline_covers_every_second_and_every_request(fake_api, capsys):
    result = load(fake_api, write_ratio=0.0, duration=2, warmup=0.1, timeline=True)

    assert [s["t"] for s in result.timeline] == [0, 1]
    assert sum(s["requests"] for s in result.timeline) == result.requests
    assert all(s["hits"] > 0 for s in result.timeline)  # the fake server counts three hits per GET
    assert WINDOW_MARKER in capsys.readouterr().err


def test_there_is_no_timeline_unless_asked_for(fake_api, capsys):
    result = load(fake_api, write_ratio=0.0)

    assert result.timeline is None
    assert WINDOW_MARKER not in capsys.readouterr().err

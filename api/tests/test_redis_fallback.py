import threading

import pytest
import redis

from api.deps import get_cache
from api.main import app
from api.services.redis_cache import CacheUnavailable, Guard, RedisCache, connect


class FlakyClient:
    """Stands in for redis.Redis: raises `failure` while it is set, and counts every call."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def get(self, key):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return "value"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


def make_cache(client, clock, retry_seconds=5.0):
    return RedisCache(client, guard=Guard(retry_seconds=retry_seconds, clock=clock))


# ---- the circuit breaker --------------------------------------------------------------------


def test_a_failed_read_is_a_miss_not_an_error(clock):
    cache = make_cache(FlakyClient(redis.ConnectionError("down")), clock)

    assert cache.get("k") is None


def test_after_a_failure_miniredis_is_not_tried_again_until_the_wait_is_over(clock):
    client = FlakyClient(redis.ConnectionError("down"))
    cache = make_cache(client, clock, retry_seconds=5)

    cache.get("k")
    assert client.calls == 1
    for _ in range(100):
        cache.get("k")
    assert client.calls == 1  # 100 more requests did not touch the network

    clock.now += 5.1
    cache.get("k")
    assert client.calls == 2  # one request was let through to check


def test_the_cache_recovers_when_miniredis_comes_back(clock):
    client = FlakyClient(redis.ConnectionError("down"))
    cache = make_cache(client, clock, retry_seconds=5)
    assert cache.get("k") is None

    client.failure = None  # MiniRedis is back
    clock.now += 5.1

    assert cache.get("k") == "value"
    assert cache.get("k") == "value"  # and the breaker is closed again


def test_a_timeout_also_opens_the_breaker(clock):
    client = FlakyClient(redis.TimeoutError("too slow"))
    cache = make_cache(client, clock)

    cache.get("k")
    cache.get("k")

    assert client.calls == 1


def test_an_error_reply_does_not_open_the_breaker(clock):
    client = FlakyClient(redis.ResponseError("WRONGTYPE"))  # the server is up, one command failed
    cache = make_cache(client, clock)

    assert cache.get("k") is None
    cache.get("k")

    assert client.calls == 2  # it was still tried the second time


def test_writes_are_skipped_not_raised_while_down(clock):
    client = connect("redis://127.0.0.1:1")  # nothing listens on port 1
    cache = make_cache(client, clock)

    cache.set("k", "v", ttl=10)
    cache.delete("k")

    assert cache.set_nx("k", "v", ttl=10) is False
    assert cache.expire("k", 10) is False


def test_incr_raises_because_there_is_no_honest_answer(clock):
    client = connect("redis://127.0.0.1:1")
    cache = make_cache(client, clock)

    with pytest.raises(CacheUnavailable):
        cache.incr("counter")


# ---- the API with a real MiniRedis that is stopped ------------------------------------------


def test_redirects_still_work_after_miniredis_is_stopped(client, redis_cache, miniredis_server):
    app.dependency_overrides[get_cache] = lambda: redis_cache
    code = client.post("/shorten", json={"url": "https://example.com/survives"}).json()["code"]
    assert client.get(f"/{code}", follow_redirects=False).status_code == 302  # cached now

    miniredis_server.stop()

    response = client.get(f"/{code}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/survives"  # from Postgres


def test_unknown_codes_are_still_404_when_miniredis_is_down(client, redis_cache, miniredis_server):
    app.dependency_overrides[get_cache] = lambda: redis_cache
    miniredis_server.stop()

    assert client.get("/nosuchcode", follow_redirects=False).status_code == 404


def test_creating_and_editing_links_works_when_miniredis_is_down(
    client, redis_cache, miniredis_server, login_as
):
    app.dependency_overrides[get_cache] = lambda: redis_cache
    miniredis_server.stop()
    alice = login_as()

    created = client.post("/shorten", json={"url": "https://example.com/a"}, headers=alice)
    code = created.json()["code"]
    edited = client.patch(f"/links/{code}", json={"url": "https://example.com/b"}, headers=alice)
    deleted = client.delete(f"/links/{code}", headers=alice)

    assert created.status_code == 201
    assert edited.status_code == 200
    assert deleted.status_code == 204


def test_a_dead_miniredis_does_not_slow_every_request(client, redis_cache, miniredis_server):
    """After the first failure the breaker is open, so later requests do not wait for timeouts."""
    app.dependency_overrides[get_cache] = lambda: redis_cache
    code = client.post("/shorten", json={"url": "https://example.com/fast"}).json()["code"]
    miniredis_server.stop()
    client.get(f"/{code}", follow_redirects=False)  # the one request that finds out

    spy = FlakyClient()  # swap in a client that counts calls: it must never be asked anything
    redis_cache._client = spy
    for _ in range(20):
        assert client.get(f"/{code}", follow_redirects=False).status_code == 302

    assert spy.calls == 0


# ---- one probe at a time ---------------------------------------------------------------------


def open_breaker(guard, clock):
    """Make the guard see a failure, then let the retry wait pass, so the next call is the probe."""

    def fail():
        raise redis.ConnectionError("down")

    with pytest.raises(CacheUnavailable):
        guard.run(fail)
    clock.now += guard._retry_seconds + 0.1


class Gate:
    """An operation that waits until the test opens the gate, and counts how often it started."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.open = threading.Event()
        self.started = threading.Semaphore(0)
        self.failure = failure
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.started.release()
        assert self.open.wait(5), "the test never opened the gate"
        if self.failure is not None:
            raise self.failure
        return "answer"


def in_thread(guard, operation):
    outcome = {}

    def work():
        try:
            outcome["value"] = guard.run(operation)
        except CacheUnavailable as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    return thread, outcome


def test_only_one_call_probes_while_the_others_keep_skipping(clock):
    guard = Guard(retry_seconds=5, clock=clock)
    open_breaker(guard, clock)
    probe = Gate(failure=redis.ConnectionError("still down"))
    thread, _ = in_thread(guard, probe)
    assert probe.started.acquire(timeout=5)  # the probe is under way, and stuck

    others = Gate()
    for _ in range(50):
        with pytest.raises(CacheUnavailable):
            guard.run(others)  # none of these may try the server

    assert probe.calls == 1
    assert others.calls == 0
    probe.open.set()
    thread.join(timeout=5)


def test_a_failed_probe_keeps_the_breaker_open_for_another_full_wait(clock):
    guard = Guard(retry_seconds=5, clock=clock)
    open_breaker(guard, clock)
    probe = Gate(failure=redis.ConnectionError("still down"))
    probe.open.set()
    with pytest.raises(CacheUnavailable):
        guard.run(probe)

    with pytest.raises(CacheUnavailable):
        guard.run(Gate())  # the wait starts again, so this one is skipped
    clock.now += 5.1
    assert guard.run(lambda: "checked") == "checked"  # and after the wait it is the next probe


def test_a_probe_that_succeeds_lets_every_call_through_again(clock):
    guard = Guard(retry_seconds=5, clock=clock)
    open_breaker(guard, clock)
    probe = Gate()
    probe.open.set()
    assert guard.run(probe) == "answer"

    later = Gate()
    later.open.set()
    assert [guard.run(later) for _ in range(5)] == ["answer"] * 5
    assert later.calls == 5


def test_a_late_success_from_before_the_outage_does_not_close_the_breaker(clock):
    """A call that started before MiniRedis died may finish after it did. That says nothing
    about whether it is back."""
    guard = Guard(retry_seconds=5, clock=clock)
    slow = Gate()
    thread, outcome = in_thread(guard, slow)  # started while MiniRedis was still up
    assert slow.started.acquire(timeout=5)

    def fail():
        raise redis.ConnectionError("died")

    with pytest.raises(CacheUnavailable):
        guard.run(fail)  # now it is known to be down
    slow.open.set()
    thread.join(timeout=5)
    assert outcome["value"] == "answer"  # the slow call did finish successfully

    with pytest.raises(CacheUnavailable):
        guard.run(Gate())  # still down


def test_an_error_reply_to_the_probe_still_shows_the_server_is_alive(clock):
    guard = Guard(retry_seconds=5, clock=clock)
    open_breaker(guard, clock)

    def wrong_type():
        raise redis.ResponseError("WRONGTYPE")

    with pytest.raises(CacheUnavailable):
        guard.run(wrong_type)  # an error, but it came from a running server

    assert guard.run(lambda: "fine") == "fine"  # so the breaker is closed again


def test_the_outage_is_logged_once_and_so_is_the_recovery(clock, caplog):
    guard = Guard(retry_seconds=5, clock=clock)
    caplog.set_level("INFO", logger="api.redis_cache")

    def fail():
        raise redis.ConnectionError("down")

    for _ in range(3):
        with pytest.raises(CacheUnavailable):
            guard.run(fail)  # the second and third are skipped
    clock.now += 5.1
    with pytest.raises(CacheUnavailable):
        guard.run(fail)  # a failed probe: nothing new to report
    clock.now += 5.1
    guard.run(lambda: "back")

    messages = [record.getMessage() for record in caplog.records]
    assert sum("unreachable" in m for m in messages) == 1
    assert sum("reachable again" in m for m in messages) == 1

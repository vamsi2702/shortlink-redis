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

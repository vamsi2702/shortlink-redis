import time

import pytest
from fastapi.testclient import TestClient

from api import config
from api.deps import get_cache
from api.main import app
from api.services.cache import InMemoryCache
from api.services.ratelimit import allow_request
from api.services.redis_cache import CacheUnavailable


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def timed_cache(clock):
    """An in-process cache whose time the test controls, installed as the app's cache."""
    cache = InMemoryCache(clock=clock)
    app.dependency_overrides[get_cache] = lambda: cache
    return cache


def make_link(client, headers=None) -> str:
    response = client.post("/shorten", json={"url": "https://example.com/limited"}, headers=headers)
    return response.json()["code"]


def hit(client, code):
    return client.get(f"/{code}", follow_redirects=False)


def test_the_101st_request_in_a_minute_gets_429(client, timed_cache):
    code = make_link(client)

    statuses = [hit(client, code).status_code for _ in range(101)]

    assert statuses[:100] == [302] * 100
    assert statuses[100] == 429


def test_the_429_says_when_to_come_back(client, timed_cache, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    code = make_link(client)
    hit(client, code)

    response = hit(client, code)

    assert response.status_code == 429
    assert response.json() == {"detail": "Too many requests"}
    assert response.headers["retry-after"] == "60"


def test_the_counter_resets_when_the_window_is_over(client, timed_cache, clock, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 3)
    code = make_link(client)
    assert [hit(client, code).status_code for _ in range(4)] == [302, 302, 302, 429]

    clock.now += 59
    assert hit(client, code).status_code == 429  # still the same minute

    clock.now += 2
    assert hit(client, code).status_code == 302  # a new window
    assert [hit(client, code).status_code for _ in range(3)] == [302, 302, 429]


def test_another_ip_address_has_its_own_allowance(client, timed_cache, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 2)
    code = make_link(client)
    other = TestClient(app, client=("203.0.113.9", 5000))
    assert [hit(client, code).status_code for _ in range(3)] == [302, 302, 429]

    assert hit(other, code).status_code == 302


def test_a_signed_in_user_is_counted_apart_from_their_ip_address(
    client, timed_cache, login_as, monkeypatch
):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 2)
    code = make_link(client)
    assert [hit(client, code).status_code for _ in range(3)] == [302, 302, 429]  # anonymous: used
    alice = login_as()

    with_token = client.get(f"/{code}", headers=alice, follow_redirects=False)

    assert with_token.status_code == 302


def test_an_invalid_token_is_counted_by_ip_address(client, timed_cache, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    code = make_link(client)
    bad = {"Authorization": "Bearer not-a-real-token"}
    client.get(f"/{code}", headers=bad, follow_redirects=False)

    assert client.get(f"/{code}", headers=bad, follow_redirects=False).status_code == 429
    assert hit(client, code).status_code == 429  # the same address, so the same counter


def test_redirects_creating_links_and_signing_in_are_counted_separately(
    client, timed_cache, monkeypatch
):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 2)
    code = make_link(client)
    assert [hit(client, code).status_code for _ in range(3)] == [302, 302, 429]

    created = client.post("/shorten", json={"url": "https://example.com/other"})

    assert created.status_code == 201  # only 1 of 2 allowed shortens used so far


def test_creating_links_is_limited(client, timed_cache, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 2)

    statuses = [
        client.post("/shorten", json={"url": f"https://example.com/{i}"}).status_code
        for i in range(3)
    ]

    assert statuses == [201, 201, 429]


def test_signing_in_is_limited(client, timed_cache, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 2)
    body = {"email": "nobody@example.com", "password": "wrong-password-1"}

    statuses = [client.post("/auth/login", json=body).status_code for _ in range(3)]

    assert statuses == [401, 401, 429]


def test_other_routes_are_not_limited(client, timed_cache, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)

    assert [client.get("/health").status_code for _ in range(5)] == [200] * 5


# ---- when the cache itself fails ----------------------------------------------------------------


class BrokenCache(InMemoryCache):
    def incr(self, key):
        raise CacheUnavailable("MiniRedis is down")


def test_requests_are_allowed_when_the_cache_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    broken = BrokenCache()
    app.dependency_overrides[get_cache] = lambda: broken
    code = make_link(client)

    assert [hit(client, code).status_code for _ in range(5)] == [302] * 5  # fail open


class Recorder:
    """A cache that records calls and answers as if the counter key expired mid-way."""

    def __init__(self) -> None:
        self.calls = []

    def set_nx(self, key, value, ttl):
        self.calls.append(("set_nx", key, ttl))
        return False  # "the key already existed" ...

    def incr(self, key):
        self.calls.append(("incr", key))
        return 1  # ... but by the time of INCR it had expired and was recreated

    def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))
        return True


def test_a_counter_that_expired_between_the_calls_gets_its_expiry_back():
    cache = Recorder()

    assert allow_request(cache, "redirect", "ip:1.2.3.4") is True

    assert ("expire", "ratelimit:redirect:ip:1.2.3.4", 60) in cache.calls


def test_a_normal_first_request_does_not_make_an_extra_call():
    cache = InMemoryCache()
    recorder_calls = []
    real_expire = cache.expire
    cache.expire = lambda key, ttl: recorder_calls.append(key) or real_expire(key, ttl)

    allow_request(cache, "redirect", "ip:1.2.3.4")
    allow_request(cache, "redirect", "ip:1.2.3.4")

    assert recorder_calls == []


# ---- with a real MiniRedis ----------------------------------------------------------------------


def test_the_limit_works_through_miniredis(client, redis_cache, miniredis_server, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 5)
    app.dependency_overrides[get_cache] = lambda: redis_cache
    code = make_link(client)

    statuses = [hit(client, code).status_code for _ in range(6)]

    assert statuses == [302] * 5 + [429]
    key = b"ratelimit:redirect:ip:testclient"
    assert miniredis_server.store.get(key) == b"6"
    assert miniredis_server.store.expire_at(key) is not None  # the window will end


def test_the_limit_resets_in_miniredis_when_the_window_ends(
    client, redis_cache, monkeypatch
):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 2)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_SECONDS", 1)
    app.dependency_overrides[get_cache] = lambda: redis_cache
    code = make_link(client)
    assert [hit(client, code).status_code for _ in range(3)] == [302, 302, 429]

    time.sleep(1.3)

    assert hit(client, code).status_code == 302


def test_requests_are_allowed_when_miniredis_is_stopped(
    client, redis_cache, miniredis_server, monkeypatch
):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    app.dependency_overrides[get_cache] = lambda: redis_cache
    code = make_link(client)
    miniredis_server.stop()

    assert [hit(client, code).status_code for _ in range(4)] == [302] * 4

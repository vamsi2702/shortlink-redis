import threading

import pytest

from api.services.cache import InMemoryCache


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cache(clock):
    return InMemoryCache(clock=clock)


def test_get_returns_none_for_a_missing_key(cache):
    assert cache.get("nope") is None


def test_set_then_get(cache):
    cache.set("a", "1")

    assert cache.get("a") == "1"


def test_set_overwrites(cache):
    cache.set("a", "1")
    cache.set("a", "2")

    assert cache.get("a") == "2"


def test_delete_removes_the_key_and_ignores_missing_ones(cache):
    cache.set("a", "1")

    cache.delete("a")
    cache.delete("never-existed")

    assert cache.get("a") is None


def test_key_expires_after_its_ttl(cache, clock):
    cache.set("a", "1", ttl=10)

    clock.now += 9.9
    assert cache.get("a") == "1"
    clock.now += 0.2
    assert cache.get("a") is None


def test_key_without_ttl_never_expires(cache, clock):
    cache.set("a", "1")

    clock.now += 10**9

    assert cache.get("a") == "1"


def test_setting_again_replaces_the_old_ttl(cache, clock):
    cache.set("a", "1", ttl=5)
    cache.set("a", "2")

    clock.now += 100

    assert cache.get("a") == "2"


def test_expire_sets_a_ttl_on_an_existing_key(cache, clock):
    cache.set("a", "1")

    assert cache.expire("a", 5) is True

    clock.now += 6
    assert cache.get("a") is None


def test_expire_on_a_missing_or_expired_key_returns_false(cache, clock):
    assert cache.expire("nope", 5) is False
    cache.set("a", "1", ttl=1)
    clock.now += 2
    assert cache.expire("a", 5) is False


def test_incr_starts_at_one_and_counts_up(cache):
    assert cache.incr("hits") == 1
    assert cache.incr("hits") == 2
    assert cache.get("hits") == "2"


def test_incr_keeps_the_existing_ttl(cache, clock):
    cache.set("hits", "5", ttl=10)

    assert cache.incr("hits") == 6

    clock.now += 11
    assert cache.get("hits") is None


def test_incr_starts_fresh_after_expiry(cache, clock):
    cache.set("hits", "5", ttl=10)
    clock.now += 11

    assert cache.incr("hits") == 1


def test_incr_on_a_non_integer_value_raises(cache):
    cache.set("a", "hello")

    with pytest.raises(ValueError):
        cache.incr("a")


def test_oldest_entry_is_evicted_when_full(clock):
    small = InMemoryCache(clock=clock, max_entries=3)
    for key in "abc":
        small.set(key, key)

    small.set("d", "d")

    assert small.get("a") is None
    assert [small.get(k) for k in "bcd"] == ["b", "c", "d"]


def test_overwriting_an_existing_key_does_not_evict_anything(clock):
    small = InMemoryCache(clock=clock, max_entries=2)
    small.set("a", "1")
    small.set("b", "2")

    small.set("a", "updated")

    assert small.get("a") == "updated"
    assert small.get("b") == "2"


def test_concurrent_increments_do_not_lose_updates(cache):
    def work():
        for _ in range(1000):
            cache.incr("counter")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert cache.get("counter") == "8000"

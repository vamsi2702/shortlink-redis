import time

from api import config
from api.deps import get_cache
from api.main import app
from api.services import links as links_service
from api.services.cache import default_cache
from api.services.redis_cache import RedisCache


def test_set_then_get(redis_cache):
    redis_cache.set("k", "value")

    assert redis_cache.get("k") == "value"


def test_a_missing_key_is_none(redis_cache):
    assert redis_cache.get("nothing") is None


def test_an_empty_string_is_stored_and_is_not_none(redis_cache):
    redis_cache.set("k", "")  # the redirect cache uses "" for "this code does not exist"

    assert redis_cache.get("k") == ""


def test_non_ascii_text_round_trips(redis_cache):
    redis_cache.set("k", "https://example.com/héllo/日本")

    assert redis_cache.get("k") == "https://example.com/héllo/日本"


def test_a_key_with_a_ttl_disappears(redis_cache):
    redis_cache.set("k", "v", ttl=0.15)
    assert redis_cache.get("k") == "v"

    time.sleep(0.4)

    assert redis_cache.get("k") is None


def test_a_key_without_a_ttl_has_no_expiry_in_miniredis(redis_cache, miniredis_server):
    redis_cache.set("k", "v")

    assert miniredis_server.store.expire_at(b"k") is None


def test_a_very_small_ttl_is_rounded_up_to_one_millisecond_not_rejected(redis_cache):
    redis_cache.set("k", "v", ttl=0.0001)  # would be PX 0, which Redis refuses


def test_delete(redis_cache):
    redis_cache.set("k", "v")

    redis_cache.delete("k")
    redis_cache.delete("never-existed")  # deleting nothing is fine

    assert redis_cache.get("k") is None


def test_set_nx_only_sets_a_missing_key(redis_cache):
    assert redis_cache.set_nx("k", "first", ttl=60) is True
    assert redis_cache.set_nx("k", "second", ttl=60) is False

    assert redis_cache.get("k") == "first"


def test_set_nx_gives_the_key_a_ttl(redis_cache, miniredis_server):
    redis_cache.set_nx("k", "v", ttl=60)

    assert miniredis_server.store.expire_at(b"k") is not None


def test_incr_counts_from_one_and_keeps_the_ttl(redis_cache, miniredis_server):
    redis_cache.set_nx("n", "0", ttl=60)
    before = miniredis_server.store.expire_at(b"n")

    assert [redis_cache.incr("n") for _ in range(3)] == [1, 2, 3]

    assert miniredis_server.store.expire_at(b"n") == before  # counting did not reset the window


def test_incr_on_a_missing_key_starts_at_one(redis_cache):
    assert redis_cache.incr("fresh") == 1


def test_expire(redis_cache, miniredis_server):
    redis_cache.set("k", "v")

    assert redis_cache.expire("k", 60) is True
    assert miniredis_server.store.expire_at(b"k") is not None
    assert redis_cache.expire("missing", 60) is False


# ---- the API serving redirects from MiniRedis ---------------------------------------------------


def test_redirects_are_cached_in_miniredis(client, redis_cache, miniredis_server, monkeypatch):
    app.dependency_overrides[get_cache] = lambda: redis_cache
    lookups = []
    real_lookup = links_service.get_link_by_code

    def counting_lookup(db, code):
        lookups.append(code)
        return real_lookup(db, code)

    monkeypatch.setattr(links_service, "get_link_by_code", counting_lookup)
    code = client.post("/shorten", json={"url": "https://example.com/in-miniredis"}).json()["code"]

    first = client.get(f"/{code}", follow_redirects=False)
    second = client.get(f"/{code}", follow_redirects=False)

    assert first.status_code == second.status_code == 302
    assert second.headers["location"] == "https://example.com/in-miniredis"
    assert lookups == [code]  # Postgres was asked once; the second answer came from MiniRedis
    assert miniredis_server.store.get(f"link:{code}".encode()) == b"https://example.com/in-miniredis"


def test_editing_a_link_clears_it_from_miniredis(client, redis_cache, login_as):
    app.dependency_overrides[get_cache] = lambda: redis_cache
    alice = login_as()
    created = client.post("/shorten", json={"url": "https://example.com/old"}, headers=alice)
    code = created.json()["code"]
    client.get(f"/{code}", follow_redirects=False)  # cached in MiniRedis now

    client.patch(f"/links/{code}", json={"url": "https://example.com/new"}, headers=alice)

    assert client.get(f"/{code}", follow_redirects=False).headers["location"] == "https://example.com/new"


# ---- choosing the cache from the settings -------------------------------------------------------


def test_without_a_miniredis_url_the_in_process_cache_is_used(monkeypatch):
    monkeypatch.setattr(config, "MINIREDIS_URL", "")

    assert get_cache() is default_cache


def test_with_a_miniredis_url_the_redis_cache_is_used(monkeypatch, miniredis_server):
    monkeypatch.setattr(config, "MINIREDIS_URL", miniredis_server.url)

    cache = get_cache()

    assert isinstance(cache, RedisCache)
    assert get_cache() is cache  # built once and shared

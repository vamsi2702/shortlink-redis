from api import config
from api.deps import get_cache, get_click_queue
from api.main import app
from api.services import links as links_service
from api.services.cache import NullCache
from api.services.clicks import NullClickQueue


def test_a_null_cache_remembers_nothing():
    cache = NullCache()

    cache.set("k", "v", ttl=60)

    assert cache.get("k") is None
    assert cache.set_nx("k", "v", ttl=60) is True  # it never holds a key, so it can always "set"
    cache.delete("k")
    assert cache.expire("k", 60) is True


def test_a_null_cache_never_limits_requests():
    cache = NullCache()

    assert {cache.incr("counter") for _ in range(1000)} == {1}  # always the first request


def test_the_cache_can_be_switched_off_by_a_setting(monkeypatch):
    monkeypatch.setattr(config, "CACHE_ENABLED", False)

    assert isinstance(get_cache(), NullCache)


def test_it_is_on_by_default():
    assert config.CACHE_ENABLED is True
    assert not isinstance(get_cache(), NullCache)


def test_without_a_cache_every_redirect_asks_postgres(client, monkeypatch):
    lookups = []
    real_lookup = links_service.get_link_by_code

    def counting_lookup(db, code):
        lookups.append(code)
        return real_lookup(db, code)

    monkeypatch.setattr(links_service, "get_link_by_code", counting_lookup)
    app.dependency_overrides[get_cache] = lambda: NullCache()
    code = client.post("/shorten", json={"url": "https://example.com/nocache"}).json()["code"]

    statuses = [client.get(f"/{code}", follow_redirects=False).status_code for _ in range(3)]

    assert statuses == [302, 302, 302]
    assert lookups == [code, code, code]  # nothing was cached


def test_clicks_can_be_switched_off_even_with_miniredis(monkeypatch, miniredis_server):
    monkeypatch.setattr(config, "MINIREDIS_URL", miniredis_server.url)
    assert not isinstance(get_click_queue(), NullClickQueue)

    monkeypatch.setattr(config, "CLICKS_ENABLED", False)

    assert isinstance(get_click_queue(), NullClickQueue)


def test_rate_limiting_can_be_switched_off(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    code = client.post("/shorten", json={"url": "https://example.com/unlimited"}).json()["code"]
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", False)

    statuses = [client.get(f"/{code}", follow_redirects=False).status_code for _ in range(5)]

    assert statuses == [302] * 5

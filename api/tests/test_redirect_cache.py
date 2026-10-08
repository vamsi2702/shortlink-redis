import pytest
from sqlalchemy import update

from api.models import Link
from api.services import links as links_service
from api.services.cache import InMemoryCache


@pytest.fixture
def db_lookups(monkeypatch):
    """Counts how many times the redirect path asks Postgres for a link."""
    calls = []
    real_lookup = links_service.get_link_by_code

    def counting_lookup(db, code):
        calls.append(code)
        return real_lookup(db, code)

    monkeypatch.setattr(links_service, "get_link_by_code", counting_lookup)
    return calls


def shorten(client, url="https://example.com/page", headers=None):
    return client.post("/shorten", json={"url": url}, headers=headers or {}).json()["code"]


def test_second_redirect_is_served_from_the_cache(client, db_lookups):
    code = shorten(client)

    first = client.get(f"/{code}", follow_redirects=False)
    second = client.get(f"/{code}", follow_redirects=False)

    assert first.status_code == second.status_code == 302
    assert first.headers["location"] == second.headers["location"]
    assert db_lookups == [code]  # Postgres was asked once, not twice


def test_redirect_fills_the_cache(client, cache):
    code = shorten(client, "https://example.com/cached")

    client.get(f"/{code}", follow_redirects=False)

    assert cache.get(links_service.link_cache_key(code)) == "https://example.com/cached"


def test_unknown_codes_are_cached_briefly_too(client, cache, db_lookups):
    first = client.get("/nosuchcode", follow_redirects=False)
    second = client.get("/nosuchcode", follow_redirects=False)

    assert first.status_code == second.status_code == 404
    assert db_lookups == ["nosuchcode"]
    assert cache.get(links_service.link_cache_key("nosuchcode")) == ""


def test_creating_a_link_clears_a_cached_not_found(client, cache, monkeypatch):
    client.get("/fresh1", follow_redirects=False)  # caches "fresh1 does not exist"
    monkeypatch.setattr(links_service, "generate_code", lambda: "fresh1")

    client.post("/shorten", json={"url": "https://example.com/now-exists"})

    response = client.get("/fresh1", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/now-exists"


def test_editing_a_link_clears_the_cache(client, login_as):
    alice = login_as()
    code = shorten(client, "https://example.com/old", alice)
    client.get(f"/{code}", follow_redirects=False)  # now cached

    client.patch(f"/links/{code}", json={"url": "https://example.com/new"}, headers=alice)

    response = client.get(f"/{code}", follow_redirects=False)
    assert response.headers["location"] == "https://example.com/new"


def test_deleting_a_link_clears_the_cache(client, login_as):
    alice = login_as()
    code = shorten(client, headers=alice)
    client.get(f"/{code}", follow_redirects=False)  # now cached

    client.delete(f"/links/{code}", headers=alice)

    assert client.get(f"/{code}", follow_redirects=False).status_code == 404


def test_cached_redirect_expires_and_is_reloaded_from_postgres(db):
    now = [0.0]
    cache = InMemoryCache(clock=lambda: now[0])
    code = links_service.create_link(db, "https://example.com/ttl").code

    assert links_service.resolve_long_url(db, cache, code) == "https://example.com/ttl"
    # change the row behind the cache's back, as if another server had edited it
    db.execute(update(Link).where(Link.code == code).values(long_url="https://example.com/changed"))
    assert links_service.resolve_long_url(db, cache, code) == "https://example.com/ttl"  # stale
    now[0] += 301  # past CACHE_TTL_SECONDS (300)

    assert links_service.resolve_long_url(db, cache, code) == "https://example.com/changed"

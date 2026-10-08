import pytest
from sqlalchemy import func, select

from api.models import Link
from api.services import links as links_service


def test_shorten_returns_code_and_saves_link(client, db):
    response = client.post("/shorten", json={"url": "https://example.com/some/long/path"})

    assert response.status_code == 201
    body = response.json()
    assert len(body["code"]) == 6
    assert body["short_url"].endswith(f"/{body['code']}")
    assert body["long_url"] == "https://example.com/some/long/path"

    saved = db.scalar(select(Link).where(Link.code == body["code"]))
    assert saved is not None
    assert saved.long_url == "https://example.com/some/long/path"


def test_same_url_twice_gets_two_different_codes(client):
    first = client.post("/shorten", json={"url": "https://example.com"}).json()
    second = client.post("/shorten", json={"url": "https://example.com"}).json()

    assert first["code"] != second["code"]


def test_bad_url_is_rejected_and_nothing_is_saved(client, db):
    count_before = db.scalar(select(func.count()).select_from(Link))

    response = client.post("/shorten", json={"url": "http://localhost:8000/admin"})

    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Link)) == count_before


def test_missing_url_field_is_rejected(client):
    assert client.post("/shorten", json={}).status_code == 422


def test_gives_up_after_too_many_collisions_and_saves_nothing(db, monkeypatch):
    db.add(Link(code="taken1", long_url="https://example.com/existing"))
    db.flush()
    monkeypatch.setattr(links_service, "generate_code", lambda: "taken1")
    count_before = db.scalar(select(func.count()).select_from(Link))

    with pytest.raises(RuntimeError, match="unique code"):
        links_service.create_link(db, "https://example.com/new")

    assert db.scalar(select(func.count()).select_from(Link)) == count_before


def test_code_collision_is_retried_with_a_new_code(client, db, monkeypatch):
    db.add(Link(code="taken1", long_url="https://example.com/existing"))
    db.flush()
    codes = iter(["taken1", "taken1", "fresh1"])
    monkeypatch.setattr(links_service, "generate_code", lambda: next(codes))

    response = client.post("/shorten", json={"url": "https://example.com/new"})

    assert response.status_code == 201
    assert response.json()["code"] == "fresh1"
    existing = db.scalar(select(Link).where(Link.code == "taken1"))
    assert existing.long_url == "https://example.com/existing"

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.models import Click, Link, User


def test_insert_and_read_back_a_link(db):
    link = Link(code="abc12", long_url="https://example.com/some/long/path")
    db.add(link)
    db.flush()

    found = db.scalar(select(Link).where(Link.code == "abc12"))

    assert found is not None
    assert found.long_url == "https://example.com/some/long/path"
    assert found.created_at is not None
    assert found.user_id is None


def test_user_owns_links_and_link_has_clicks(db):
    user = User(email="a@example.com", password_hash="not-a-real-hash")
    link = Link(code="xyz99", long_url="https://example.com", user=user)
    link.clicks.append(Click())
    link.clicks.append(Click())
    db.add(user)
    db.flush()

    assert [lk.code for lk in user.links] == ["xyz99"]
    assert len(link.clicks) == 2


def test_link_code_must_be_unique(db):
    db.add(Link(code="dup", long_url="https://example.com/1"))
    db.flush()
    db.add(Link(code="dup", long_url="https://example.com/2"))

    with pytest.raises(IntegrityError):
        db.flush()


def test_user_email_must_be_unique(db):
    db.add(User(email="same@example.com", password_hash="x"))
    db.flush()
    db.add(User(email="same@example.com", password_hash="y"))

    with pytest.raises(IntegrityError):
        db.flush()

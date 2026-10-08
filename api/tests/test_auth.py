from datetime import timedelta

import pytest
from sqlalchemy import select

from api.models import User
from api.services.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

PASSWORD = "correct-horse-battery"


def register(client, email="alice@example.com", password=PASSWORD):
    return client.post("/auth/register", json={"email": email, "password": password})


def test_register_creates_a_user_without_exposing_the_hash(client, db):
    response = register(client)

    assert response.status_code == 201
    assert set(response.json()) == {"id", "email"}
    saved = db.scalar(select(User).where(User.email == "alice@example.com"))
    assert saved.password_hash != PASSWORD
    assert saved.password_hash.startswith("$2")  # a bcrypt hash


def test_register_lowercases_the_email(client, db):
    register(client, email="Alice@Example.COM")

    assert db.scalar(select(User).where(User.email == "alice@example.com")) is not None


def test_register_twice_with_same_email_returns_409(client):
    register(client)

    assert register(client).status_code == 409
    assert register(client, email="ALICE@example.com").status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "a@example.com", "password": "short"},
        {"email": "a@example.com", "password": "x" * 73},
        {"email": "a@example.com"},
        {"password": PASSWORD},
    ],
)
def test_register_rejects_bad_input(client, payload):
    assert client.post("/auth/register", json=payload).status_code == 422


def test_login_returns_a_token_that_works(client):
    register(client)

    response = client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD})

    assert response.status_code == 200
    token = response.json()["access_token"]
    assert response.json()["token_type"] == "bearer"
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.com"


def test_login_is_case_insensitive_on_email(client):
    register(client)

    response = client.post("/auth/login", json={"email": "ALICE@example.com", "password": PASSWORD})

    assert response.status_code == 200


def test_wrong_password_and_unknown_email_give_the_same_error(client):
    register(client)

    wrong_password = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "wrong-password"}
    )
    unknown_email = client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_me_requires_a_token(client):
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("token", ["garbage", "a.b.c", ""])
def test_me_rejects_garbage_tokens(client, token):
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_me_rejects_an_expired_token(client):
    user_id = register(client).json()["id"]
    expired = create_access_token(user_id, expires_in=timedelta(seconds=-1))

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401


def test_token_for_a_deleted_user_is_rejected(client):
    token = create_access_token(user_id=999_999_999)

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_password_hashing_roundtrip():
    password_hash = hash_password("s3cret-passphrase")

    assert verify_password("s3cret-passphrase", password_hash)
    assert not verify_password("wrong", password_hash)
    assert not verify_password("anything", "not-a-valid-hash")


def test_tokens_cannot_be_forged_with_another_secret():
    import jwt

    forged = jwt.encode({"sub": "1", "exp": 9_999_999_999}, "some-other-secret" * 3, "HS256")

    assert decode_access_token(forged) is None
    assert decode_access_token(create_access_token(7)) == 7

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api import config
from api.db import engine
from api.deps import get_cache, get_click_queue, get_db
from api.main import app
from api.services.cache import InMemoryCache
from api.services.clicks import InMemoryClickQueue, RedisClickQueue
from api.services.redis_cache import RedisCache, connect
from miniredis.testing import start_server_thread


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    """bcrypt is slow on purpose; the lowest cost keeps the test suite quick."""
    monkeypatch.setattr(config, "BCRYPT_ROUNDS", 4)


@pytest.fixture
def db():
    """A session whose work is rolled back after each test, so tests never leave data behind."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def cache():
    """A fresh, empty cache for each test, so tests cannot affect each other."""
    return InMemoryCache()


@pytest.fixture
def miniredis_server():
    """A real MiniRedis running in a background thread, stopped after the test."""
    server = start_server_thread()
    yield server
    server.stop()


@pytest.fixture
def redis_cache(miniredis_server):
    """The API's RedisCache connected to that MiniRedis."""
    return RedisCache(connect(miniredis_server.url))


@pytest.fixture
def client(db, cache):
    """A test client that uses the rolled-back `db` session and the per-test `cache`."""
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_cache] = lambda: cache
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def click_queue(client):
    """An in-process click queue installed as the app's queue, so tests can see what was queued."""
    queue = InMemoryClickQueue()
    app.dependency_overrides[get_click_queue] = lambda: queue
    return queue


@pytest.fixture
def redis_click_queue(miniredis_server):
    """A click queue stored in the test MiniRedis."""
    return RedisClickQueue(connect(miniredis_server.url))


@pytest.fixture
def login_as(client):
    """login_as("bob@example.com") registers (if needed), logs in, and returns auth headers."""

    def _login_as(email: str = "alice@example.com", password: str = "correct-horse-battery"):
        client.post("/auth/register", json={"email": email, "password": password})
        response = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _login_as

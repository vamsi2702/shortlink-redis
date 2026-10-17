import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select

from api import config, worker
from api.db import engine
from api.deps import get_cache, get_click_queue
from api.main import app
from api.models import Click
from api.services import links as links_service
from api.services.clicks import InMemoryClickQueue, make_event, save_clicks
from api.services.redis_cache import CacheUnavailable


def make_link(db, url="https://example.com/worker"):
    return links_service.create_link(db, url)


def click_count(db, link=None) -> int:
    query = select(func.count()).select_from(Click)
    if link is not None:
        query = query.where(Click.link_id == link.id)
    return db.scalar(query)


@pytest.fixture
def queue():
    return InMemoryClickQueue()


# ---- save_clicks --------------------------------------------------------------------------------


def test_save_clicks_stores_each_event_with_its_link_and_time(db):
    link = make_link(db)
    events = [make_event(link.code, 1_760_000_000_000), make_event(link.code, 1_760_000_005_500)]

    assert save_clicks(db, events) == 2

    times = db.scalars(select(Click.clicked_at).where(Click.link_id == link.id)).all()
    assert sorted(times) == [
        datetime.fromtimestamp(1_760_000_000, tz=UTC),
        datetime.fromtimestamp(1_760_000_005.5, tz=UTC),
    ]


def test_save_clicks_sorts_events_into_the_right_links(db):
    first, second = make_link(db, "https://example.com/1"), make_link(db, "https://example.com/2")

    save_clicks(db, [make_event(first.code), make_event(second.code), make_event(first.code)])

    assert click_count(db, first) == 2
    assert click_count(db, second) == 1


def test_save_clicks_skips_unknown_links_and_garbage(db):
    link = make_link(db)
    events = [make_event(link.code), make_event("deleted1"), "garbage", "", make_event(link.code)]

    assert save_clicks(db, events) == 2

    assert click_count(db) == 2


def test_save_clicks_with_nothing_to_save(db):
    assert save_clicks(db, []) == 0
    assert save_clicks(db, ["garbage"]) == 0


def test_a_batch_is_written_with_few_sql_statements(db):
    code = make_link(db).code
    events = [make_event(code) for _ in range(100)]
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.split()[0:3])

    event.listen(engine, "before_cursor_execute", record)
    try:
        save_clicks(db, events)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    kinds = [s[0] for s in statements if s[0] in ("SELECT", "INSERT")]
    assert kinds == ["SELECT", "INSERT"]  # one lookup of the link ids, one INSERT for all 100 rows
    assert click_count(db) == 100


# ---- drain_once ---------------------------------------------------------------------------------


def test_drain_once_moves_a_batch_from_the_queue_to_postgres(db, queue):
    link = make_link(db)
    for _ in range(5):
        queue.push(make_event(link.code))

    result = worker.drain_once(queue, db)

    assert result == worker.Drained(taken=5, saved=5)
    assert click_count(db, link) == 5
    assert len(queue.events) == 0


def test_drain_once_takes_at_most_one_batch(db, queue, monkeypatch):
    monkeypatch.setattr(config, "CLICK_BATCH_SIZE", 100)
    link = make_link(db)
    for _ in range(250):
        queue.push(make_event(link.code))

    sizes = [worker.drain_once(queue, db).taken for _ in range(4)]

    assert sizes == [100, 100, 50, 0]
    assert click_count(db, link) == 250


def test_drain_once_on_an_empty_queue(db, queue):
    assert worker.drain_once(queue, db) == worker.Drained(0, 0)


def test_events_for_deleted_links_are_taken_but_not_saved(db, queue):
    link = make_link(db)
    queue.push(make_event(link.code))
    queue.push(make_event("gonelink"))

    assert worker.drain_once(queue, db) == worker.Drained(taken=2, saved=1)


def test_events_are_put_back_when_postgres_fails(db, queue, monkeypatch):
    link = make_link(db)
    for number in range(3):
        queue.push(make_event(link.code, 1_760_000_000_000 + number))

    def failing_save(db, events):
        raise RuntimeError("postgres is down")

    monkeypatch.setattr(worker, "save_clicks", failing_save)
    with pytest.raises(RuntimeError):
        worker.drain_once(queue, db)

    assert len(queue.events) == 3  # nothing was lost
    monkeypatch.undo()
    assert worker.drain_once(queue, db) == worker.Drained(taken=3, saved=3)  # and it works later


def test_a_dead_queue_raises_so_the_loop_can_wait(db):
    class DeadQueue:
        def pop_batch(self, count):
            raise CacheUnavailable("down")

    with pytest.raises(CacheUnavailable):
        worker.drain_once(DeadQueue(), db)


# ---- the loop -----------------------------------------------------------------------------------


@contextmanager
def shared_session(db):
    yield db  # the test's rolled-back session; not closed after each batch


def run_worker(queue, db, stop):
    thread = threading.Thread(
        target=worker.run, args=(queue, stop, lambda: shared_session(db)), daemon=True
    )
    thread.start()
    return thread


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_the_loop_saves_clicks_as_they_arrive_and_stops_on_request(db, queue, monkeypatch):
    monkeypatch.setattr(config, "CLICK_WORKER_INTERVAL_SECONDS", 0.05)
    link = make_link(db)
    stop = threading.Event()
    thread = run_worker(queue, db, stop)

    for _ in range(7):
        queue.push(make_event(link.code))
    assert wait_until(lambda: len(queue.events) == 0)
    stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert click_count(db, link) == 7


def test_the_loop_drains_what_is_left_when_asked_to_stop(db, queue, monkeypatch):
    monkeypatch.setattr(config, "CLICK_WORKER_INTERVAL_SECONDS", 30)  # it would sleep a long time
    link = make_link(db)
    stop = threading.Event()
    thread = run_worker(queue, db, stop)
    time.sleep(0.2)  # the loop is now waiting out its 30 s sleep
    for _ in range(4):
        queue.push(make_event(link.code))

    stop.set()  # wakes the sleep; the final drain should save the 4 events
    thread.join(timeout=5)

    assert click_count(db, link) == 4


def test_the_loop_survives_the_queue_being_down_and_recovers(db, monkeypatch):
    monkeypatch.setattr(config, "CLICK_WORKER_INTERVAL_SECONDS", 0.05)
    link = make_link(db)

    class FlakyQueue(InMemoryClickQueue):
        down = True

        def pop_batch(self, count):
            if self.down:
                raise CacheUnavailable("down")
            return super().pop_batch(count)

    queue = FlakyQueue()
    queue.push(make_event(link.code))
    stop = threading.Event()
    thread = run_worker(queue, db, stop)

    time.sleep(0.3)
    assert thread.is_alive()  # it kept going while the queue was down
    assert click_count(db, link) == 0
    queue.down = False
    assert wait_until(lambda: len(queue.events) == 0)
    stop.set()
    thread.join(timeout=5)

    assert click_count(db, link) == 1


# ---- end to end: redirects in, rows out ---------------------------------------------------------


def test_click_counts_in_postgres_match_the_number_of_redirects(
    client, db, redis_cache, redis_click_queue
):
    app.dependency_overrides[get_cache] = lambda: redis_cache
    app.dependency_overrides[get_click_queue] = lambda: redis_click_queue
    code = client.post("/shorten", json={"url": "https://example.com/e2e"}).json()["code"]
    other = client.post("/shorten", json={"url": "https://example.com/e2e-2"}).json()["code"]

    for _ in range(37):
        assert client.get(f"/{code}", follow_redirects=False).status_code == 302
    for _ in range(5):
        client.get(f"/{other}", follow_redirects=False)
    while worker.drain_once(redis_click_queue, db).taken:  # what the worker does, batch by batch
        pass

    link = links_service.get_link_by_code(db, code)
    assert click_count(db, link) == 37
    assert click_count(db, links_service.get_link_by_code(db, other)) == 5


def test_main_refuses_to_start_without_a_miniredis_url(monkeypatch):
    monkeypatch.setattr(config, "MINIREDIS_URL", "")

    assert worker.main() == 1

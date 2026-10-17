import time
from datetime import UTC, datetime

import pytest

from api import config
from api.deps import get_cache, get_click_queue
from api.main import app
from api.services.clicks import (
    NullClickQueue,
    RedisClickQueue,
    make_event,
    parse_event,
    record_click,
)
from api.services.redis_cache import CacheUnavailable, connect


def make_link(client, url="https://example.com/clicked") -> str:
    return client.post("/shorten", json={"url": url}).json()["code"]


def hit(client, code):
    return client.get(f"/{code}", follow_redirects=False)


# ---- events -------------------------------------------------------------------------------------


def test_an_event_round_trips():
    event = make_event("abc123", at_ms=1_760_000_000_123)

    assert event == "abc123 1760000000123"
    assert parse_event(event) == ("abc123", datetime(2025, 10, 9, 8, 53, 20, 123000, tzinfo=UTC))


def test_an_event_defaults_to_the_current_time():
    before = time.time_ns() // 1_000_000

    code, at = parse_event(make_event("abc123"))

    assert code == "abc123"
    assert before <= at.timestamp() * 1000 <= time.time_ns() // 1_000_000 + 1


def test_bad_events_are_rejected_not_raised():
    for bad in ["", "abc123", "abc123 ", " 123", "abc123 notanumber", "abc123 -5", "a " + "9" * 40]:
        assert parse_event(bad) is None, bad


# ---- redirects leave events ---------------------------------------------------------------------


def test_a_redirect_queues_one_click_event(client, click_queue):
    code = make_link(client)

    hit(client, code)

    assert len(click_queue.events) == 1
    queued_code, at = parse_event(click_queue.events[0])
    assert queued_code == code
    assert abs(at.timestamp() - time.time()) < 5


def test_every_redirect_is_counted_including_cached_ones(client, click_queue):
    code = make_link(client)

    for _ in range(3):
        assert hit(client, code).status_code == 302  # the 2nd and 3rd come from the cache

    assert len(click_queue.events) == 3


def test_unknown_codes_are_not_queued(client, click_queue):
    assert hit(client, "nosuchcode").status_code == 404
    assert hit(client, "nosuchcode").status_code == 404  # the cached "not found" answer too

    assert len(click_queue.events) == 0


def test_refused_requests_are_not_counted_as_clicks(client, click_queue, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 2)
    code = make_link(client)

    statuses = [hit(client, code).status_code for _ in range(4)]

    assert statuses == [302, 302, 429, 429]
    assert len(click_queue.events) == 2


def test_a_queue_that_fails_does_not_break_the_redirect(client):
    class BrokenQueue:
        def push(self, event):
            return False

        def pop_batch(self, count):
            raise CacheUnavailable("down")

    app.dependency_overrides[get_click_queue] = lambda: BrokenQueue()
    code = make_link(client)

    assert hit(client, code).status_code == 302


def test_without_miniredis_the_default_queue_drops_clicks(client):
    code = make_link(client)

    assert hit(client, code).status_code == 302
    assert get_click_queue().__class__ is NullClickQueue
    assert get_click_queue().push("abc 1") is False
    assert get_click_queue().pop_batch(10) == []


# ---- the real queue: a list in MiniRedis --------------------------------------------------------


def test_events_appear_in_the_miniredis_list(
    client, redis_cache, redis_click_queue, miniredis_server
):
    app.dependency_overrides[get_cache] = lambda: redis_cache
    app.dependency_overrides[get_click_queue] = lambda: redis_click_queue
    code = make_link(client)

    for _ in range(3):
        hit(client, code)

    queued = miniredis_server.store.get(b"clicks")
    assert len(queued) == 3
    assert all(parse_event(event.decode())[0] == code for event in queued)


def test_events_come_out_oldest_first(redis_click_queue):
    for number in range(5):
        redis_click_queue.push(f"code{number} {number}")

    assert redis_click_queue.pop_batch(2) == ["code0 0", "code1 1"]
    assert redis_click_queue.pop_batch(10) == ["code2 2", "code3 3", "code4 4"]


def test_popping_from_an_empty_queue_gives_an_empty_list(redis_click_queue):
    assert redis_click_queue.pop_batch(10) == []


def test_a_batch_is_removed_from_the_queue(redis_click_queue, miniredis_server):
    redis_click_queue.push("a 1")
    redis_click_queue.push("b 2")

    redis_click_queue.pop_batch(2)

    assert miniredis_server.store.get(b"clicks") is None  # an emptied list is deleted


def test_two_consumers_never_get_the_same_event(redis_click_queue, miniredis_server):
    other = RedisClickQueue(connect(miniredis_server.url))
    for number in range(10):
        redis_click_queue.push(f"c{number} {number}")

    first = redis_click_queue.pop_batch(4)
    second = other.pop_batch(4)
    third = redis_click_queue.pop_batch(4)

    assert len(first + second + third) == 10
    assert len(set(first + second + third)) == 10


def test_push_reports_failure_instead_of_raising(miniredis_server):
    queue = RedisClickQueue(connect(miniredis_server.url))
    miniredis_server.stop()

    assert queue.push("a 1") is False
    assert record_click(queue, "abc123") is False


def test_popping_from_a_dead_queue_raises_so_the_worker_can_wait(miniredis_server):
    queue = RedisClickQueue(connect(miniredis_server.url))
    miniredis_server.stop()

    with pytest.raises(CacheUnavailable):
        queue.pop_batch(10)

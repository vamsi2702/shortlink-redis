import time
from collections import deque
from datetime import UTC, datetime
from typing import Protocol

import redis
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from api import config
from api.models import Click, Link
from api.services.redis_cache import CacheUnavailable, Guard


class ClickQueue(Protocol):
    """Where redirects leave click events for the worker to save later."""

    def push(self, event: str) -> bool:
        """Add an event. Returns False if it could not be queued (the click is then lost)."""
        ...

    def pop_batch(self, count: int) -> list[str]:
        """Take up to `count` events, oldest first. Raises CacheUnavailable if the queue is down."""
        ...


class RedisClickQueue:
    """The queue is a MiniRedis list: LPUSH adds on the left, RPOP takes from the right, so the
    oldest event comes out first (first in, first out)."""

    def __init__(self, client: redis.Redis, guard: Guard | None = None) -> None:
        self._client = client
        self._guard = guard or Guard()

    def push(self, event: str) -> bool:
        try:
            self._guard.run(lambda: self._client.lpush(config.CLICK_QUEUE_KEY, event))
        except CacheUnavailable:
            return False
        return True

    def pop_batch(self, count: int) -> list[str]:
        events = self._guard.run(lambda: self._client.rpop(config.CLICK_QUEUE_KEY, count))
        return events or []  # RPOP with a count answers null when the list does not exist


class NullClickQueue:
    """Used when there is no MiniRedis: clicks are not recorded."""

    def push(self, event: str) -> bool:
        return False

    def pop_batch(self, count: int) -> list[str]:
        return []


class InMemoryClickQueue:
    """A queue inside the process, for tests."""

    def __init__(self) -> None:
        self.events: deque[str] = deque()

    def push(self, event: str) -> bool:
        self.events.appendleft(event)
        return True

    def pop_batch(self, count: int) -> list[str]:
        batch = []
        while self.events and len(batch) < count:
            batch.append(self.events.pop())
        return batch


# ---- events -------------------------------------------------------------------------------------
# An event is "<code> <unix time in ms>", for example "abc123 1760000000000". Codes are letters and
# digits, so the space is safe. The time is in the event because the worker may save the click
# seconds later, and the click should be stamped with when it happened.


def make_event(code: str, at_ms: int | None = None) -> str:
    return f"{code} {time.time_ns() // 1_000_000 if at_ms is None else at_ms}"


def parse_event(event: str) -> tuple[str, datetime] | None:
    """The code and click time in an event, or None if it is not a valid event."""
    code, _, millis = event.rpartition(" ")
    if not code or not millis.isdigit():
        return None
    try:
        return code, datetime.fromtimestamp(int(millis) / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def record_click(queue: ClickQueue, code: str) -> bool:
    """Queue one click on a link. Never raises: a click that cannot be queued must not break the
    redirect."""
    return queue.push(make_event(code))


def save_clicks(db: Session, events: list[str]) -> int:
    """Write a batch of click events to the clicks table; returns how many rows were saved.

    Two database round trips for the whole batch: one query finds the ids of all the links, one
    INSERT adds all the rows. Events that are malformed, or for links that no longer exist, are
    skipped.
    """
    parsed = [p for p in map(parse_event, events) if p is not None]
    if not parsed:
        return 0
    codes = {code for code, _ in parsed}
    found = db.execute(select(Link.code, Link.id).where(Link.code.in_(codes)))
    link_ids = {code: link_id for code, link_id in found}
    rows = [
        {"link_id": link_ids[code], "clicked_at": at} for code, at in parsed if code in link_ids
    ]
    if rows:
        db.execute(insert(Click), rows)
    db.commit()
    return len(rows)

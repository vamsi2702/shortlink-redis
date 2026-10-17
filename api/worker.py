"""The click worker: moves click events from the MiniRedis queue into the Postgres clicks table.

Run it next to the API:   python -m api.worker

The API only drops a small note in the queue when someone follows a short link, so redirects stay
fast. This program empties the queue in batches, which is much cheaper for Postgres than one write
per click.
"""

import logging
import signal
import sys
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import NamedTuple

from sqlalchemy.orm import Session

from api import config
from api.db import SessionLocal
from api.services.clicks import ClickQueue, RedisClickQueue, save_clicks
from api.services.redis_cache import CacheUnavailable, connect

log = logging.getLogger("api.worker")


class Drained(NamedTuple):
    taken: int  # events removed from the queue
    saved: int  # rows added to the clicks table (fewer if some links were deleted)


def drain_once(queue: ClickQueue, db: Session) -> Drained:
    """Move one batch from the queue to Postgres.

    The events are removed from the queue before they are saved, so if saving fails they are put
    back (at the other end; their order does not matter because each carries its own time). Raises
    CacheUnavailable if the queue cannot be reached, and re-raises a failed save.
    """
    events = queue.pop_batch(config.CLICK_BATCH_SIZE)
    if not events:
        return Drained(0, 0)
    try:
        saved = save_clicks(db, events)
    except Exception:
        db.rollback()
        lost = sum(not queue.push(event) for event in events)
        if lost:
            log.error("%d click events could not be put back and are lost", lost)
        raise
    return Drained(len(events), saved)


def run(
    queue: ClickQueue,
    stop: threading.Event,
    session_factory: Callable[[], AbstractContextManager[Session]] = SessionLocal,
) -> None:
    """Drain the queue until `stop` is set, then drain what is left."""
    interval = config.CLICK_WORKER_INTERVAL_SECONDS
    while not stop.is_set():
        try:
            with session_factory() as db:
                result = drain_once(queue, db)
        except CacheUnavailable as exc:
            log.warning("the click queue is unreachable (%s)", exc)
            stop.wait(interval)
            continue
        except Exception:
            log.exception("could not save a batch of clicks; it was put back in the queue")
            stop.wait(interval)
            continue
        if result.taken:
            log.info("saved %d clicks (%d events taken)", result.saved, result.taken)
        if result.taken < config.CLICK_BATCH_SIZE:  # the queue is empty now; a full batch means
            stop.wait(interval)  # there may be more, so go straight back for it
    _drain_remaining(queue, session_factory)


def _drain_remaining(
    queue: ClickQueue, session_factory: Callable[[], AbstractContextManager[Session]]
) -> None:
    """On shutdown, save what is still queued so a restart loses nothing it did not have to."""
    try:
        while True:
            with session_factory() as db:
                if not drain_once(queue, db).taken:
                    return
    except Exception:
        log.exception("could not finish draining the click queue on shutdown")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not config.MINIREDIS_URL:
        log.error("MINIREDIS_URL is not set, so there is no click queue to read")
        return 1
    stop = threading.Event()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_number, lambda *_: stop.set())
    log.info("click worker started (queue %r at %s)", config.CLICK_QUEUE_KEY, config.MINIREDIS_URL)
    run(RedisClickQueue(connect(config.MINIREDIS_URL)), stop)
    log.info("click worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

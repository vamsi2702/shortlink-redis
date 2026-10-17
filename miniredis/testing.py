"""Helpers for tests (here and in the API) that need a real MiniRedis server running."""

import asyncio
import threading

from miniredis.server import MiniRedisServer, start_server
from miniredis.store import Store


class ServerThread(threading.Thread):
    """Runs MiniRedis on its own event loop in a background thread, on a free port.

    Blocking clients such as redis-py or plain sockets can then talk to it from the test.
    """

    def __init__(self, **options) -> None:
        super().__init__(daemon=True)
        self.store = Store()
        self.redis: MiniRedisServer | None = None  # the running server, for peeking inside
        self.address: tuple[str, int] | None = None
        self.ready = threading.Event()
        self._options = options  # passed on to start_server, e.g. expire_interval
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def url(self) -> str:
        assert self.address is not None, "the server has not started"
        return f"redis://{self.address[0]}:{self.address[1]}"

    def run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self.redis = await start_server("127.0.0.1", 0, store=self.store, **self._options)
        self.address = self.redis.address
        self.ready.set()
        await self._stop_event.wait()
        await self.redis.close()

    def stop(self) -> None:
        if not self.is_alive():  # already stopped (a test may stop it itself)
            return
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self.join(timeout=10)


def start_server_thread(**options) -> ServerThread:
    thread = ServerThread(**options)
    thread.start()
    assert thread.ready.wait(timeout=5), "server did not start"
    return thread

import asyncio
import socket
import threading
from collections import deque

import pytest

from miniredis.resp import Parser, encode_command
from miniredis.server import start_server
from miniredis.store import Store


class ServerThread(threading.Thread):
    """Runs MiniRedis on its own event loop in a background thread, on a free port.

    Blocking clients such as redis-py or plain sockets can then talk to it from the test.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.store = Store()
        self.address: tuple[str, int] | None = None
        self.ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    def run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        server = await start_server("127.0.0.1", 0, store=self.store)
        self.address = server.sockets[0].getsockname()[:2]
        self.ready.set()
        await self._stop_event.wait()
        server.close()
        if hasattr(server, "close_clients"):  # Python 3.13+: don't wait for lingering clients
            server.close_clients()
        await asyncio.wait_for(server.wait_closed(), timeout=5)

    def stop(self) -> None:
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self.join(timeout=10)


class RespClient:
    """A tiny blocking client for tests: sends bytes, reads parsed replies."""

    def __init__(self, address: tuple[str, int], timeout: float = 5.0) -> None:
        self.sock = socket.create_connection(address, timeout=timeout)
        self.parser = Parser()
        self._replies: deque[object] = deque()

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read(self, timeout: float | None = None) -> object:
        """The next reply. Raises TimeoutError if none arrives, ConnectionError if closed."""
        if timeout is not None:
            self.sock.settimeout(timeout)
        while not self._replies:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("the server closed the connection")
            self.parser.feed(data)
            self._replies.extend(self.parser.messages())
        return self._replies.popleft()

    def call(self, *words: object) -> object:
        self.send_raw(encode_command(*words))
        return self.read()

    def server_closed_connection(self, timeout: float = 2.0) -> bool:
        """True if the server hangs up (no more replies are pending) within `timeout` seconds."""
        try:
            self.read(timeout=timeout)
        except ConnectionError:
            return True
        except TimeoutError:
            return False
        return False

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def server():
    thread = ServerThread()
    thread.start()
    assert thread.ready.wait(timeout=5), "server did not start"
    yield thread
    thread.stop()


@pytest.fixture
def connect(server):
    """connect() opens a new client to the test server; all are closed after the test."""
    clients: list[RespClient] = []

    def _connect() -> RespClient:
        client = RespClient(server.address)
        clients.append(client)
        return client

    yield _connect
    for client in clients:
        client.close()


@pytest.fixture
def client(connect):
    return connect()


REAL_REDIS_ADDRESS = ("127.0.0.1", 6379)


@pytest.fixture
def connect_real():
    """connect_real() opens a client to the reference Redis (docker compose up -d redis).

    The test is skipped if it is not running.
    """
    clients: list[RespClient] = []

    def _connect() -> RespClient:
        try:
            client = RespClient(REAL_REDIS_ADDRESS, timeout=2)
        except OSError:
            pytest.skip("real Redis is not running on port 6379 (docker compose up -d redis)")
        clients.append(client)
        return client

    yield _connect
    for client in clients:
        client.close()

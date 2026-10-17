"""A thread-per-connection MiniRedis, for the concurrency experiment. Not the real one.

    python -m miniredis.threaded_server --port 6381

It runs exactly the same commands as the asyncio server (miniredis/server.py); only the way clients
are served differs. Here every connection gets its own thread, which blocks while it waits for
that client. The store is NOT thread-safe, so a single lock makes commands run one at a time. That
is what the asyncio server gets for free from having one thread that never awaits inside a command.

Use it to compare the two designs (bench/concurrency.py). The asyncio server is the one to run.
"""

import argparse
import itertools
import logging
import socket
import threading

from miniredis.commands import ClientState, Context, Stats
from miniredis.pubsub import PubSub
from miniredis.resp import Parser, ProtocolError, RespError, encode
from miniredis.server import (
    _NO_REPLY,
    DEFAULT_HOST,
    DEFAULT_PORT,
    RECV_SIZE,
    encode_reply,
    run_message,
)
from miniredis.store import Store

log = logging.getLogger("miniredis.threaded")

EXPIRE_INTERVAL = 0.1
EXPIRE_BATCH = 1000
THREAD_STACK_BYTES = 512 * 1024  # small, so a thousand connections do not need gigabytes
_client_ids = itertools.count(1)


class ThreadedServer:
    def __init__(
        self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, store: Store | None = None
    ) -> None:
        self.host, self.port = host, port
        self.store = store if store is not None else Store()
        self.stats = Stats()
        self.pubsub = PubSub()
        self.lock = threading.Lock()  # held while a command runs: one at a time, whoever sent it
        self._listener: socket.socket | None = None
        self._connections: set[socket.socket] = set()
        self._closing = threading.Event()

    def start(self) -> None:
        """Listen, and serve clients on background threads."""
        threading.stack_size(THREAD_STACK_BYTES)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(1024)
        self._listener = listener
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._expire_loop, daemon=True).start()

    @property
    def address(self) -> tuple[str, int]:
        assert self._listener is not None, "call start() first"
        return self._listener.getsockname()[:2]

    def extra_info(self) -> dict[str, dict[str, object]]:
        return {"server": {"tcp_port": self.address[1]}}

    def serve_forever(self) -> None:
        """Block until close() is called (the server itself runs on background threads)."""
        self._closing.wait()

    def close(self) -> None:
        self._closing.set()
        if self._listener is not None:
            self._listener.close()
        for connection in list(self._connections):
            try:
                connection.close()
            except OSError:
                pass

    # ---- background threads ---------------------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._closing.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:  # the listener was closed
                return
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._connections.add(connection)
            threading.Thread(target=self._serve_client, args=(connection,), daemon=True).start()

    def _expire_loop(self) -> None:
        while not self._closing.wait(EXPIRE_INTERVAL):
            with self.lock:
                self.store.purge_expired(limit=EXPIRE_BATCH)

    # ---- one thread per client ------------------------------------------------------------------

    def _serve_client(self, connection: socket.socket) -> None:
        send_lock = threading.Lock()  # a published message may be sent by another client's thread
        client = ClientState(id=next(_client_ids))
        ctx = Context(
            store=self.store,
            client=client,
            stats=self.stats,
            extra_info=self.extra_info,
            pubsub=self.pubsub,
        )

        def send(data: bytes) -> bool:
            try:
                with send_lock:
                    connection.sendall(data)
            except OSError:
                return False
            return True

        client.push = send
        parser = Parser(allow_inline=True)
        with self.lock:
            self.stats.connections_received += 1
            self.stats.connected_clients += 1
        try:
            while not client.close_after_reply:
                data = connection.recv(RECV_SIZE)  # this thread waits here for this client
                if not data:
                    break
                parser.feed(data)
                replies: list[bytes] = []
                try:
                    with self.lock:
                        for message in parser.messages():
                            reply = run_message(ctx, message)
                            if reply is not _NO_REPLY:
                                replies.append(encode_reply(reply))
                            if client.close_after_reply:
                                break
                except ProtocolError as exc:
                    replies.append(encode(RespError(f"ERR Protocol error: {exc}")))
                    client.close_after_reply = True
                if replies and not send(b"".join(replies)):
                    break
        except OSError:
            pass
        finally:
            client.push = None
            with self.lock:
                self.pubsub.unsubscribe_all(client)
                self.stats.connected_clients -= 1
            self._connections.discard(connection)
            try:
                connection.close()
            except OSError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Thread-per-connection MiniRedis (experiment)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = ThreadedServer(args.host, args.port)
    server.start()
    log.info("threaded MiniRedis listening on %s:%s", *server.address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")


if __name__ == "__main__":
    main()

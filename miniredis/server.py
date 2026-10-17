"""MiniRedis: a Redis-compatible server. Start it with `python -m miniredis.server`.

Each connected client gets one `handle_client` task on a single event loop. The loop is:

    bytes from the socket -> RESP parser -> command layer -> RESP encoder -> bytes to the socket

(resp.py is the parser and encoder, commands.py the commands). Next to the clients, a
background task runs on the same loop: the expiry cleaner.

MiniRedis keeps everything in memory: when the server stops, the data is gone.
"""

import argparse
import asyncio
import itertools
import logging

from miniredis import commands
from miniredis.commands import ClientState, Context, Replies, Stats
from miniredis.pubsub import PubSub
from miniredis.resp import Parser, ProtocolError, RespError, encode
from miniredis.store import Store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6380  # next to Redis's 6379, so both can run side by side
RECV_SIZE = 65536
MAX_PUSH_BUFFER = 32 * 1024 * 1024  # unsent published messages a client may fall behind by
EXPIRE_INTERVAL = 0.1  # seconds between runs of the expiry cleaner
EXPIRE_BATCH = 1000  # heap entries the cleaner looks at per run

log = logging.getLogger("miniredis.server")

_client_ids = itertools.count(1)
_NO_REPLY = object()


def run_message(ctx: Context, message: object) -> object:
    """Turn one parsed message into a reply. This function must stay synchronous (no await)."""
    if not isinstance(message, list) or not all(isinstance(word, bytes) for word in message):
        raise ProtocolError("expected an array of bulk strings")
    if not message:
        return _NO_REPLY  # an empty command is ignored, as in real Redis
    return commands.execute(ctx, message)


def encode_reply(reply: object) -> bytes:
    try:
        if isinstance(reply, Replies):  # several separate answers to one command
            return b"".join(encode(part) for part in reply)
        return encode(reply)
    except (ValueError, TypeError):  # a handler returned something RESP cannot represent
        log.exception("could not encode reply %r", reply)
        return encode(RespError("ERR internal error"))


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, server: "MiniRedisServer"
) -> None:
    peer = writer.get_extra_info("peername")
    ctx = Context(
        store=server.store,
        client=ClientState(id=next(_client_ids)),
        stats=server.stats,
        extra_info=server.extra_info,
        pubsub=server.pubsub,
    )

    def push(data: bytes) -> bool:
        """Send a published message to this client, even though it is waiting for input. A
        client that does not read what it is sent would make the server's memory grow without
        limit, so one that falls too far behind is disconnected (real Redis does the same)."""
        if writer.transport.get_write_buffer_size() > MAX_PUSH_BUFFER:
            log.warning("client %d cannot keep up with its messages; disconnecting", ctx.client.id)
            writer.transport.abort()
            return False
        writer.write(data)
        return True

    ctx.client.push = push
    parser = Parser(allow_inline=True)
    log.info("client %d connected: %s", ctx.client.id, peer)
    server.stats.connections_received += 1
    server.stats.connected_clients += 1
    try:
        while not ctx.client.close_after_reply:
            data = await reader.read(RECV_SIZE)  # the only place we wait for this client
            if not data:  # the client closed the connection
                break
            parser.feed(data)
            replies: list[bytes] = []
            try:
                for message in parser.messages():
                    reply = run_message(ctx, message)
                    if reply is not _NO_REPLY:
                        replies.append(encode_reply(reply))
                    if ctx.client.close_after_reply:
                        break
            except ProtocolError as exc:
                # the bytes are not valid RESP, so we cannot know where the next command starts
                replies.append(encode(RespError(f"ERR Protocol error: {exc}")))
                ctx.client.close_after_reply = True
            if replies:
                writer.write(b"".join(replies))  # all the replies for this read, in one go
            await writer.drain()  # waits if the client is slow to read our replies
    except ConnectionError as exc:  # reset by the peer
        log.info("client %d error: %s", ctx.client.id, exc)
    finally:
        ctx.client.push = None
        server.pubsub.unsubscribe_all(ctx.client)  # a client that left stops receiving messages
        server.stats.connected_clients -= 1
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
        log.info("client %d disconnected", ctx.client.id)


class MiniRedisServer:
    """Everything one running MiniRedis consists of: the listening socket, the shared store, and
    the background task that works next to the clients on the same loop."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        store: Store | None = None,
        expire_interval: float = EXPIRE_INTERVAL,
    ) -> None:
        self.host = host
        self.port = port
        self.store = store if store is not None else Store()
        self.stats = Stats()
        self.pubsub = PubSub()
        self._expire_interval = expire_interval
        self._server: asyncio.Server | None = None
        self._expire_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port)
        self._expire_task = asyncio.create_task(self._expire_loop())

    @property
    def address(self) -> tuple[str, int]:
        """The (host, port) it is really listening on (useful when started with port 0)."""
        assert self._server is not None, "call start() first"
        return self._server.sockets[0].getsockname()[:2]

    def extra_info(self) -> dict[str, dict[str, object]]:
        """Facts only the server knows, for the INFO command."""
        return {"server": {"tcp_port": self.address[1]}}

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await handle_client(reader, writer, self)

    async def _expire_loop(self) -> None:
        """Kill expired keys even if nobody ever reads them again."""
        while True:
            try:
                self.store.purge_expired(limit=EXPIRE_BATCH)
                # more keys are already due: let the clients have a turn, then carry on at once
                pause = 0 if self.store.has_due_expiry() else self._expire_interval
            except Exception:  # the cleaner must never die, whatever happens
                log.exception("expiry cleaner failed")
                pause = self._expire_interval
            await asyncio.sleep(pause)

    async def serve_forever(self) -> None:
        assert self._server is not None, "call start() first"
        await self._server.serve_forever()

    async def close(self) -> None:
        if self._expire_task is not None:
            self._expire_task.cancel()
            await asyncio.gather(self._expire_task, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            if hasattr(self._server, "close_clients"):  # Python 3.13+: don't wait for clients
                self._server.close_clients()
            await asyncio.wait_for(self._server.wait_closed(), timeout=5)


async def start_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    store: Store | None = None,
    *,
    expire_interval: float = EXPIRE_INTERVAL,
) -> MiniRedisServer:
    server = MiniRedisServer(host, port, store=store, expire_interval=expire_interval)
    await server.start()
    return server


async def serve(host: str, port: int) -> None:
    server = await start_server(host, port)
    log.info("MiniRedis listening on %s:%s (Ctrl+C to stop)", *server.address)
    try:
        await server.serve_forever()
    finally:
        await server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniRedis, a Redis-compatible server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(serve(args.host, args.port))
    except KeyboardInterrupt:
        log.info("stopping")


if __name__ == "__main__":
    main()

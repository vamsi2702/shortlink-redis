"""MiniRedis: a Redis-compatible server. Start it with `python -m miniredis.server`.

Each connected client gets one `handle_client` task on a single event loop. The loop is:

    bytes from the socket -> RESP parser -> command layer -> RESP encoder -> bytes to the socket

(step 2.2 built the socket part, 2.4 the parser and encoder, 2.5 to 2.7 the commands). Next to the
clients, background tasks run on the same loop: the expiry cleaner (3.2) and the log syncer (3.5).
"""

import argparse
import asyncio
import itertools
import logging
from pathlib import Path

from miniredis import commands
from miniredis.aof import FSYNC_POLICIES, AppendOnlyFile, ReplayResult, replay
from miniredis.commands import ClientState, Context
from miniredis.resp import Parser, ProtocolError, RespError, encode
from miniredis.store import Store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6380  # next to Redis's 6379, so both can run side by side
DEFAULT_AOF_PATH = "data/miniredis.aof"
RECV_SIZE = 65536
EXPIRE_INTERVAL = 0.1  # seconds between runs of the expiry cleaner
EXPIRE_BATCH = 1000  # heap entries the cleaner looks at per run
AOF_SYNC_INTERVAL = 1.0  # seconds between fsyncs with the "everysec" policy

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
        return encode(reply)
    except (ValueError, TypeError):  # a handler returned something RESP cannot represent
        log.exception("could not encode reply %r", reply)
        return encode(RespError("ERR internal error"))


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, server: "MiniRedisServer"
) -> None:
    peer = writer.get_extra_info("peername")
    ctx = Context(store=server.store, client=ClientState(id=next(_client_ids)), aof=server.aof)
    parser = Parser(allow_inline=True)
    log.info("client %d connected: %s", ctx.client.id, peer)
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
            replies = server.commit(replies)  # the log is written BEFORE any reply leaves
            if replies:
                writer.write(b"".join(replies))
            await writer.drain()  # waits if the client is slow to read our replies
    except ConnectionError as exc:  # reset by the peer
        log.info("client %d error: %s", ctx.client.id, exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
        log.info("client %d disconnected", ctx.client.id)


class MiniRedisServer:
    """Everything one running MiniRedis consists of: the listening socket, the shared store, the
    append-only file, and the background tasks that work next to the clients on the same loop."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        store: Store | None = None,
        aof_path: str | Path | None = None,
        appendfsync: str = "everysec",
        expire_interval: float = EXPIRE_INTERVAL,
    ) -> None:
        self.host = host
        self.port = port
        self.store = store if store is not None else Store()
        self.aof: AppendOnlyFile | None = None
        if aof_path is not None:
            self.aof = AppendOnlyFile(aof_path, appendfsync)
        self.aof_load: ReplayResult | None = None  # what replaying the file found, once started
        self._expire_interval = expire_interval
        self._server: asyncio.Server | None = None
        self._expire_task: asyncio.Task[None] | None = None
        self._aof_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def load_aof(self) -> ReplayResult:
        """Rebuild the data from the append-only file. Runs before any client is accepted."""
        assert self.aof is not None
        replay_context = Context(store=self.store, client=ClientState(id=0))  # not logged again
        self.store.loading = True  # nothing expires and past times are only remembered
        try:
            result = replay(self.aof.path, lambda words: commands.execute(replay_context, words))
        finally:
            self.store.loading = False
        log.info(
            "loaded %d commands from %s (%d keys)", result.commands, self.aof.path, len(self.store)
        )
        return result

    async def start(self) -> None:
        if self.aof is not None:
            self.aof_load = self.load_aof()
            self.aof.open()
            aof = self.aof
            # a key that dies of old age is deleted in the log too, at that moment
            self.store.on_expire = lambda key: aof.append([b"DEL", key])
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port)
        self._expire_task = asyncio.create_task(self._expire_loop())
        if self.aof is not None and self.aof.fsync_policy == "everysec":
            self._aof_task = asyncio.create_task(self._aof_loop())

    @property
    def address(self) -> tuple[str, int]:
        """The (host, port) it is really listening on (useful when started with port 0)."""
        assert self._server is not None, "call start() first"
        return self._server.sockets[0].getsockname()[:2]

    def commit(self, replies: list[bytes]) -> list[bytes]:
        """Write the changes logged by this batch of commands to the file. Returns the replies to
        send: the same ones, or, if the file cannot be written, an error in place of each, because
        the client must not be told a change is safe when it may not be."""
        if self.aof is None:
            return replies
        try:
            self.aof.commit()
        except OSError as exc:
            log.error("cannot write the append-only file: %s", exc)
            text = str(exc).replace("\r", " ").replace("\n", " ")
            return [encode(RespError(f"MISCONF Errors writing to the AOF file: {text}"))] * len(
                replies
            )
        return replies

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await handle_client(reader, writer, self)

    async def _expire_loop(self) -> None:
        """Step 3.2: kill expired keys even if nobody ever reads them again."""
        while True:
            try:
                self.store.purge_expired(limit=EXPIRE_BATCH)
                if self.aof is not None:
                    self.aof.commit()  # the DELs written for the keys that just died
                # more keys are already due: let the clients have a turn, then carry on at once
                pause = 0 if self.store.has_due_expiry() else self._expire_interval
            except Exception:  # the cleaner must never die, whatever happens
                log.exception("expiry cleaner failed")
                pause = self._expire_interval
            await asyncio.sleep(pause)

    async def _aof_loop(self) -> None:
        """Step 3.5: with the 'everysec' policy, fsync the log once a second. The fsync itself
        runs in a thread, so a slow disk never freezes the event loop."""
        assert self.aof is not None
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=AOF_SYNC_INTERVAL)
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(self.aof.sync)
            except OSError:
                log.exception("fsync of the append-only file failed")

    async def serve_forever(self) -> None:
        assert self._server is not None, "call start() first"
        await self._server.serve_forever()

    async def close(self) -> None:
        self._stopping.set()
        if self._expire_task is not None:
            self._expire_task.cancel()
        tasks = [t for t in (self._expire_task, self._aof_task) if t is not None]
        await asyncio.gather(*tasks, return_exceptions=True)  # the syncer finishes its last fsync
        if self._server is not None:
            self._server.close()
            if hasattr(self._server, "close_clients"):  # Python 3.13+: don't wait for clients
                self._server.close_clients()
            await asyncio.wait_for(self._server.wait_closed(), timeout=5)
        if self.aof is not None:
            self.aof.close()


async def start_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    store: Store | None = None,
    *,
    aof_path: str | Path | None = None,
    appendfsync: str = "everysec",
) -> MiniRedisServer:
    server = MiniRedisServer(host, port, store=store, aof_path=aof_path, appendfsync=appendfsync)
    await server.start()
    return server


async def serve(
    host: str, port: int, aof_path: str | Path | None, appendfsync: str = "everysec"
) -> None:
    server = await start_server(host, port, aof_path=aof_path, appendfsync=appendfsync)
    log.info("MiniRedis listening on %s:%s (Ctrl+C to stop)", *server.address)
    try:
        await server.serve_forever()
    finally:
        await server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniRedis, a Redis-compatible server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--aof-file", default=DEFAULT_AOF_PATH, help="where changes are logged (%(default)s)"
    )
    parser.add_argument(
        "--no-aof", action="store_true", help="keep everything in memory only; lose it on exit"
    )
    parser.add_argument(
        "--appendfsync", choices=FSYNC_POLICIES, default="everysec", help="see miniredis/aof.py"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    aof_path = None if args.no_aof else args.aof_file
    try:
        asyncio.run(serve(args.host, args.port, aof_path, args.appendfsync))
    except KeyboardInterrupt:
        log.info("stopping")


if __name__ == "__main__":
    main()

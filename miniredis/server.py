"""MiniRedis: a Redis-compatible server. Start it with `python -m miniredis.server`.

Each connected client gets one `handle_client` task on a single event loop. The loop is:

    bytes from the socket -> RESP parser -> command layer -> RESP encoder -> bytes to the socket

(step 2.2 built the socket part, 2.4 the parser and encoder, 2.5 to 2.7 the commands).
"""

import argparse
import asyncio
import itertools
import logging

from miniredis import commands
from miniredis.commands import ClientState, Context
from miniredis.resp import Parser, ProtocolError, RespError, encode
from miniredis.store import Store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6380  # next to Redis's 6379, so both can run side by side
RECV_SIZE = 65536

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
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, store: Store
) -> None:
    peer = writer.get_extra_info("peername")
    ctx = Context(store=store, client=ClientState(id=next(_client_ids)))
    parser = Parser(allow_inline=True)
    log.info("client %d connected: %s", ctx.client.id, peer)
    try:
        while not ctx.client.close_after_reply:
            data = await reader.read(RECV_SIZE)  # the only place we wait for this client
            if not data:  # the client closed the connection
                break
            parser.feed(data)
            try:
                for message in parser.messages():
                    reply = run_message(ctx, message)
                    if reply is not _NO_REPLY:
                        writer.write(encode_reply(reply))
                    if ctx.client.close_after_reply:
                        break
            except ProtocolError as exc:
                # the bytes are not valid RESP, so we cannot know where the next command starts
                writer.write(encode(RespError(f"ERR Protocol error: {exc}")))
                ctx.client.close_after_reply = True
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


async def start_server(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, store: Store | None = None
) -> asyncio.Server:
    store = store if store is not None else Store()

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_client(reader, writer, store)

    return await asyncio.start_server(on_connect, host, port)


async def serve(host: str, port: int) -> None:
    server = await start_server(host, port)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    log.info("MiniRedis listening on %s (Ctrl+C to stop)", addresses)
    async with server:
        await server.serve_forever()


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

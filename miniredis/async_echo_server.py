"""An asyncio TCP echo server: many clients at once on a single thread (step 2.2).

Same job as echo_server.py, but a quiet client no longer blocks the others.
"""

import argparse
import asyncio
import logging

from miniredis.echo_server import DEFAULT_HOST, DEFAULT_PORT, RECV_SIZE

log = logging.getLogger("miniredis.echo")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Echo everything one client sends. The loop runs one of these per connected client."""
    peer = writer.get_extra_info("peername")
    log.info("client connected: %s", peer)
    try:
        while True:
            # pauses THIS client's code until bytes arrive; other clients run in the meantime
            data = await reader.read(RECV_SIZE)
            if not data:  # empty bytes means the client closed the connection
                break
            writer.write(data)  # queues the bytes to send; does not wait
            await writer.drain()  # pauses if the client is slow to read, so memory can't pile up
    except ConnectionError as exc:  # the client crashed or the connection was reset
        log.info("client error: %s", exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
        log.info("client disconnected: %s", peer)


async def start_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> asyncio.Server:
    # start_server does socket(), bind() and listen() for us, and calls handle_client for
    # every accepted connection
    return await asyncio.start_server(handle_client, host, port)


async def serve(host: str, port: int) -> None:
    server = await start_server(host, port)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets)
    log.info("async echo server listening on %s (Ctrl+C to stop)", addresses)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Asyncio TCP echo server")
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

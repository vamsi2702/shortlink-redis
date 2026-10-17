"""A forwarding proxy that prints every byte passing through it.

    client  <--->  proxy_sniffer (port 6381)  <--->  real Redis (port 6379)

Point any Redis client at port 6381 and watch exactly what it says and what the server answers.
Handy to learn the handshake `redis-py` performs, which MiniRedis has to understand too.

    python -m miniredis.proxy_sniffer --listen-port 6381 --target-port 6379
"""

import argparse
import asyncio
import logging
from collections.abc import Callable

log = logging.getLogger("miniredis.sniffer")

CLIENT_TO_SERVER = "client -> server"
SERVER_TO_CLIENT = "server -> client"

Observer = Callable[[str, bytes], None]


async def pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str, observe: Observer
) -> None:
    """Copy bytes from reader to writer, telling `observe` about each chunk."""
    try:
        while data := await reader.read(65536):
            observe(direction, data)
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()  # closing our side lets the other direction notice and finish too


def make_handler(target_host: str, target_port: int, observe: Observer):
    async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
        try:
            server_reader, server_writer = await asyncio.open_connection(target_host, target_port)
        except OSError as exc:
            log.error("cannot reach %s:%s: %s", target_host, target_port, exc)
            client_writer.close()
            return
        await asyncio.gather(
            pipe(client_reader, server_writer, CLIENT_TO_SERVER, observe),
            pipe(server_reader, client_writer, SERVER_TO_CLIENT, observe),
        )

    return handle


def print_observer(direction: str, data: bytes) -> None:
    log.info("%s  %r", direction, data)


async def serve(listen_host: str, listen_port: int, target_host: str, target_port: int) -> None:
    handler = make_handler(target_host, target_port, print_observer)
    server = await asyncio.start_server(handler, listen_host, listen_port)
    log.info("sniffing %s:%s -> %s:%s", listen_host, listen_port, target_host, target_port)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the bytes between a client and a server")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=6381)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=6379)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(serve(args.listen_host, args.listen_port, args.target_host, args.target_port))
    except KeyboardInterrupt:
        log.info("stopping")


if __name__ == "__main__":
    main()

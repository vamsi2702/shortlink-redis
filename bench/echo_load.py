"""Throw many simultaneous clients at an echo server and report how it copes.

    python -m bench.echo_load --clients 50 --hold 0.1

Each client connects, sends some messages (checking every reply), waits `hold` seconds with the
connection still open, then disconnects. A server that serves one client at a time needs about
clients x hold seconds; a server that handles clients concurrently needs about `hold`.
"""

import argparse
import asyncio
import statistics
import time

from miniredis.echo_server import DEFAULT_HOST, DEFAULT_PORT


async def one_client(
    host: str, port: int, index: int, messages: int, hold: float, timeout: float
) -> float | None:
    """Seconds this client took, or None if it failed (timeout, refused, or a wrong reply)."""
    start = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                for i in range(messages):
                    payload = f"client-{index}-message-{i}".encode()
                    writer.write(payload)
                    await writer.drain()
                    if await reader.readexactly(len(payload)) != payload:
                        return None  # got somebody else's data, or garbage
                await asyncio.sleep(hold)
            finally:
                writer.close()
    except (TimeoutError, OSError, asyncio.IncompleteReadError):
        return None
    return time.perf_counter() - start


async def run_load(
    host: str, port: int, clients: int, messages: int = 5, hold: float = 0.0, timeout: float = 30.0
) -> list[float | None]:
    return list(
        await asyncio.gather(
            *(one_client(host, port, i, messages, hold, timeout) for i in range(clients))
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Echo server load test")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--messages", type=int, default=5, help="messages per client")
    parser.add_argument("--hold", type=float, default=0.1, help="seconds a client stays connected")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds before giving up")
    args = parser.parse_args()

    start = time.perf_counter()
    results = asyncio.run(
        run_load(args.host, args.port, args.clients, args.messages, args.hold, args.timeout)
    )
    total = time.perf_counter() - start

    done = [r for r in results if r is not None]
    print(f"clients: {args.clients}   succeeded: {len(done)}   failed: {args.clients - len(done)}")
    print(f"total time: {total:.2f}s")
    if done:
        print(f"per-client time: median {statistics.median(done):.2f}s, max {max(done):.2f}s")


if __name__ == "__main__":
    main()

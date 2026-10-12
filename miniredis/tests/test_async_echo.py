import asyncio
import contextlib
import socket
import struct
import time

import pytest
import pytest_asyncio

from bench.echo_load import run_load
from miniredis.async_echo_server import start_server

HOST = "127.0.0.1"


@pytest_asyncio.fixture
async def server():
    """The asyncio echo server on a free port; yields (host, port)."""
    srv = await start_server(HOST, 0)  # port 0 = the OS picks any free port
    yield HOST, srv.sockets[0].getsockname()[1]
    srv.close()
    await asyncio.wait_for(srv.wait_closed(), timeout=5)


@contextlib.asynccontextmanager
async def client(address):
    reader, writer = await asyncio.open_connection(*address)
    try:
        yield reader, writer
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def echo(reader, writer, text: str) -> str:
    data = text.encode()
    writer.write(data)
    await writer.drain()
    return (await reader.readexactly(len(data))).decode()


async def test_server_echoes_what_the_client_sends(server):
    async with client(server) as (reader, writer):
        assert await echo(reader, writer, "hello") == "hello"


async def test_many_messages_on_one_connection(server):
    async with client(server) as (reader, writer):
        for text in ["one", "two", "a longer message with spaces", "ünïcödé ✓"]:
            assert await echo(reader, writer, text) == text


async def test_an_idle_client_does_not_block_others(server):
    """The blocking server made the second client wait. This one must not."""
    async with client(server) as (reader1, writer1):
        assert await echo(reader1, writer1, "ping") == "ping"  # client 1 is connected, then silent
        async with client(server) as (reader2, writer2):
            async with asyncio.timeout(1):
                assert await echo(reader2, writer2, "hello") == "hello"


async def test_fifty_clients_connected_at_once_all_get_their_own_replies(server):
    async with contextlib.AsyncExitStack() as stack:
        # first connect all 50 and keep them open...
        connections = [await stack.enter_async_context(client(server)) for _ in range(50)]
        # ...then have them all talk at the same time
        replies = await asyncio.gather(
            *(echo(r, w, f"client number {i}") for i, (r, w) in enumerate(connections))
        )

    assert replies == [f"client number {i}" for i in range(50)]


async def test_replies_are_never_mixed_up_between_clients(server):
    results = await run_load(*server, clients=50, messages=20, hold=0.0, timeout=10)

    assert all(r is not None for r in results)


async def test_concurrent_clients_take_about_as_long_as_one(server):
    start = time.perf_counter()
    results = await run_load(*server, clients=50, messages=2, hold=0.2, timeout=10)
    elapsed = time.perf_counter() - start

    assert all(r is not None for r in results)
    # a one-at-a-time server would need 50 x 0.2 = 10 seconds
    assert elapsed < 3


async def test_large_message_is_echoed_in_full(server):
    payload = bytes(range(256)) * 4096  # 1 MiB

    async with client(server) as (reader, writer):

        async def send():
            writer.write(payload)
            await writer.drain()

        # send and receive at the same time, or both sides' buffers fill up and wait on each other
        _, received = await asyncio.gather(send(), reader.readexactly(len(payload)))

    assert received == payload


async def test_server_survives_a_client_that_crashes(server):
    sock = socket.create_connection(server, timeout=5)
    sock.sendall(b"hi")
    # SO_LINGER with a zero timeout makes close() send a TCP reset, like a crashing program
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()

    async with client(server) as (reader, writer):
        assert await echo(reader, writer, "still alive") == "still alive"


async def test_load_tool_reports_failure_when_nothing_is_listening():
    results = await run_load(HOST, 1, clients=3, messages=1, timeout=2)  # port 1: nothing there

    assert results == [None, None, None]


@pytest.mark.parametrize("clients", [1, 10])
async def test_load_tool_counts_every_client(server, clients):
    results = await run_load(*server, clients=clients, messages=1)

    assert len(results) == clients

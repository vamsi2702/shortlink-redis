import asyncio
import contextlib

import pytest
import pytest_asyncio

from miniredis.async_echo_server import start_server
from miniredis.proxy_sniffer import CLIENT_TO_SERVER, SERVER_TO_CLIENT, make_handler
from miniredis.resp_explorer import command_to_resp, describe, send_and_receive, unescape

HOST = "127.0.0.1"


# ---- encoding a command ---------------------------------------------------------------------


def test_set_a_1_is_the_27_bytes_worked_out_by_hand():
    payload = command_to_resp(["SET", "a", "1"])

    assert payload == b"*3\r\n$3\r\nSET\r\n$1\r\na\r\n$1\r\n1\r\n"
    assert len(payload) == 27


def test_single_word_command():
    assert command_to_resp(["PING"]) == b"*1\r\n$4\r\nPING\r\n"


def test_empty_argument_is_a_zero_length_string():
    assert command_to_resp(["SET", "k", ""]) == b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$0\r\n\r\n"


def test_lengths_count_bytes_not_characters():
    assert command_to_resp(["GET", "é"]) == b"*2\r\n$3\r\nGET\r\n$2\r\n\xc3\xa9\r\n"


def test_argument_containing_crlf_is_safe_because_of_the_length_prefix():
    assert command_to_resp(["SET", "k", "a\r\nb"]).endswith(b"$4\r\na\r\nb\r\n")


# ---- typing bytes by hand -------------------------------------------------------------------


def test_unescape_turns_typed_backslash_sequences_into_real_bytes():
    assert unescape(r"*1\r\n$4\r\nPING\r\n") == b"*1\r\n$4\r\nPING\r\n"


def test_unescape_rejects_non_ascii_text():
    with pytest.raises(UnicodeEncodeError):
        unescape("é")


# ---- explaining RESP ------------------------------------------------------------------------


def test_describe_explains_a_command_line_by_line():
    lines = describe(command_to_resp(["SET", "a", "1"]))

    assert len(lines) == 7
    assert "array of 3 items" in lines[0]
    assert "string of 3 bytes follows" in lines[1]
    assert "the string's bytes" in lines[2]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (b"+OK\r\n", "simple string"),
        (b"-ERR bad\r\n", "error"),
        (b":42\r\n", "integer"),
        (b"$-1\r\n", "null (no value)"),
        (b"*-1\r\n", "null array"),
    ],
)
def test_describe_recognises_each_reply_type(reply, expected):
    (line,) = describe(reply)

    assert expected in line


def test_describe_flags_a_message_cut_short():
    lines = describe(b"$5\r\nhel")

    assert "incomplete" in lines[-1]


# ---- sending to a server --------------------------------------------------------------------


@pytest_asyncio.fixture
async def echo_server():
    server = await start_server(HOST, 0)
    yield HOST, server.sockets[0].getsockname()[1]
    server.close()
    await asyncio.wait_for(server.wait_closed(), timeout=5)


async def test_send_and_receive_returns_what_the_server_sent_back(echo_server):
    payload = command_to_resp(["PING"])

    # the client call blocks, so run it in a thread and keep the server's event loop free
    reply = await asyncio.to_thread(send_and_receive, *echo_server, payload, 0.1)

    assert reply == payload  # an echo server answers with exactly what it received


# ---- the sniffing proxy ---------------------------------------------------------------------


@pytest_asyncio.fixture
async def proxy(echo_server):
    """A sniffer proxy in front of the echo server; yields (address, list of observed chunks)."""
    seen: list[tuple[str, bytes]] = []
    handler = make_handler(*echo_server, lambda direction, data: seen.append((direction, data)))
    server = await asyncio.start_server(handler, HOST, 0)
    yield (HOST, server.sockets[0].getsockname()[1]), seen
    server.close()
    await asyncio.wait_for(server.wait_closed(), timeout=5)


async def test_proxy_passes_bytes_through_unchanged_in_both_directions(proxy):
    address, seen = proxy
    reader, writer = await asyncio.open_connection(*address)
    try:
        writer.write(b"hello")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(5), timeout=2) == b"hello"
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()

    assert (CLIENT_TO_SERVER, b"hello") in seen
    assert (SERVER_TO_CLIENT, b"hello") in seen


async def test_proxy_serves_several_clients_at_once(proxy):
    address, _ = proxy

    async def talk(i: int) -> bytes:
        reader, writer = await asyncio.open_connection(*address)
        try:
            writer.write(f"client {i}".encode())
            await writer.drain()
            return await asyncio.wait_for(reader.readexactly(len(f"client {i}")), timeout=2)
        finally:
            writer.close()

    replies = await asyncio.gather(*(talk(i) for i in range(10)))

    assert replies == [f"client {i}".encode() for i in range(10)]


async def test_proxy_closes_the_client_if_the_target_is_unreachable():
    handler = make_handler(HOST, 1, lambda direction, data: None)  # nothing listens on port 1
    server = await asyncio.start_server(handler, HOST, 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection(HOST, port)
        assert await asyncio.wait_for(reader.read(100), timeout=5) == b""  # connection closed
        writer.close()
    finally:
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)

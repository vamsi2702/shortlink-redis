import socket
import struct
import threading

import pytest

from miniredis.echo_client import echo, recv_exactly
from miniredis.echo_server import create_listener, serve_forever


@pytest.fixture
def server():
    """Runs the echo server in a background thread on a free port; yields (host, port)."""
    listener = create_listener("127.0.0.1", 0)  # port 0 = the OS picks any free port
    stop = threading.Event()
    thread = threading.Thread(target=serve_forever, args=(listener, stop, 0.05), daemon=True)
    thread.start()
    yield listener.getsockname()[:2]
    stop.set()
    thread.join(timeout=5)
    listener.close()


def connect(address) -> socket.socket:
    sock = socket.create_connection(address, timeout=5)
    return sock


def test_server_echoes_what_the_client_sends(server):
    with connect(server) as sock:
        assert echo(sock, "hello") == "hello"


def test_many_messages_on_one_connection(server):
    with connect(server) as sock:
        for text in ["one", "two", "three", "a longer message with spaces", "ünïcödé ✓"]:
            assert echo(sock, text) == text


def test_binary_data_survives_unchanged(server):
    payload = bytes(range(256)) * 4
    with connect(server) as sock:
        sock.sendall(payload)

        assert recv_exactly(sock, len(payload)) == payload


def test_clients_are_served_one_after_another(server):
    for i in range(3):
        with connect(server) as sock:
            assert echo(sock, f"client {i}") == f"client {i}"


def test_server_survives_a_client_that_connects_and_leaves_silently(server):
    connect(server).close()

    with connect(server) as sock:
        assert echo(sock, "still alive") == "still alive"


def test_server_survives_a_client_that_crashes(server):
    sock = connect(server)
    sock.sendall(b"hi")
    # SO_LINGER with a zero timeout makes close() send a TCP reset, like a crashing program
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()

    with connect(server) as other:
        assert echo(other, "still alive") == "still alive"


def test_blocking_server_makes_a_second_client_wait(server):
    """The limitation the asyncio server removes: one slow client blocks everyone behind it."""
    first = connect(server)
    assert echo(first, "ping") == "ping"  # the server is now busy serving `first`

    second = connect(server)  # the connection is accepted by the OS, but not by our server
    second.sendall(b"hello")
    second.settimeout(0.5)
    with pytest.raises(TimeoutError):
        second.recv(100)  # no echo: the server is stuck waiting on `first`

    first.close()  # now the server is free to accept `second`
    second.settimeout(5)
    assert recv_exactly(second, 5) == b"hello"
    second.close()


def test_recv_exactly_collects_a_reply_that_arrives_in_pieces():
    left, right = socket.socketpair()
    with left, right:
        right.sendall(b"hel")
        timer = threading.Timer(0.1, right.sendall, args=(b"lo",))
        timer.start()

        assert recv_exactly(left, 5) == b"hello"
        timer.join()


def test_recv_exactly_raises_if_the_connection_closes_early():
    left, right = socket.socketpair()
    with left:
        right.sendall(b"hi")
        right.close()

        with pytest.raises(ConnectionError):
            recv_exactly(left, 5)

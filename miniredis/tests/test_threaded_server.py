import socket
import threading
import time

import pytest
import redis

from miniredis.resp import Parser, encode_command
from miniredis.threaded_server import ThreadedServer


@pytest.fixture
def threaded():
    server = ThreadedServer("127.0.0.1", 0)
    server.start()
    yield server
    server.close()


def client_for(address) -> redis.Redis:
    return redis.Redis(host=address[0], port=address[1], protocol=2)


def raw_connection(address) -> socket.socket:
    connection = socket.create_connection(address, timeout=5)
    return connection


def read_replies(connection, count) -> list:
    parser = Parser()
    replies: list = []
    while len(replies) < count:
        data = connection.recv(65536)
        if not data:
            break
        parser.feed(data)
        replies.extend(parser.messages())
    return replies


def test_the_basics_work(threaded):
    r = client_for(threaded.address)

    assert r.ping() is True
    assert r.set("name", "vamsi") is True
    assert r.get("name") == b"vamsi"
    assert r.incr("hits") == 1
    assert r.lpush("queue", "a", "b") == 2
    assert r.rpop("queue", 2) == [b"a", b"b"]
    assert r.hset("h", "f", "v") == 1
    assert r.hgetall("h") == {b"f": b"v"}


def test_it_answers_exactly_like_the_asyncio_server(threaded, server):
    """The same commands to both servers must give the same answers."""
    commands = [
        ("SET", "a", "1"),
        ("GET", "a"),
        ("GET", "missing"),
        ("INCR", "a"),
        ("INCRBY", "a", "40"),
        ("SET", "t", "v", "EX", "100"),
        ("TTL", "t"),
        ("TTL", "a"),
        ("TTL", "nokey"),
        ("EXISTS", "a", "a", "nokey"),
        ("DEL", "a", "a"),
        ("LPUSH", "list", "x", "y", "z"),
        ("LRANGE", "list", "0", "-1"),
        ("RPOP", "list", "2"),
        ("RPOP", "nolist", "2"),
        ("LLEN", "list"),
        ("HSET", "h", "f1", "v1", "f2", "v2"),
        ("HGET", "h", "f2"),
        ("TYPE", "h"),
        ("GET", "h"),
        ("INCR", "t"),
        ("SET", "x", "y", "NX", "XX"),
        ("NOSUCHCOMMAND", "arg"),
        ("GET",),
        ("ECHO", "hello"),
    ]
    first = client_for(threaded.address)
    second = client_for(server.address)

    def ask(client, *words):
        try:
            return client.execute_command(*words)
        except redis.ResponseError as exc:
            return f"error: {exc}"

    for words in commands:
        assert ask(first, *words) == ask(second, *words), words


def test_many_threads_incrementing_one_counter_lose_nothing(threaded):
    """The lock is what makes this exact: without it, two threads can read the same old value."""
    threads_count, per_thread = 50, 200
    failures = []

    def work():
        try:
            r = client_for(threaded.address)
            for _ in range(per_thread):
                r.incr("counter")
        except Exception as exc:  # report from the main thread
            failures.append(exc)

    threads = [threading.Thread(target=work) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not failures
    assert client_for(threaded.address).get("counter") == str(threads_count * per_thread).encode()


def test_two_hundred_clients_at_once_all_get_answers(threaded):
    connections = [raw_connection(threaded.address) for _ in range(200)]
    try:
        for number, connection in enumerate(connections):
            connection.sendall(encode_command("SET", f"k{number}", number))
        replies = [read_replies(connection, 1)[0] for connection in connections]
        assert all(reply == "OK" for reply in replies)
        assert len(threaded.store) == 200
    finally:
        for connection in connections:
            connection.close()


def test_a_published_message_reaches_a_subscriber_on_another_thread(threaded):
    subscriber = raw_connection(threaded.address)
    subscriber.sendall(encode_command("SUBSCRIBE", "news"))
    assert read_replies(subscriber, 1) == [[b"subscribe", b"news", 1]]

    delivered = client_for(threaded.address).publish("news", "hello")

    assert delivered == 1
    assert read_replies(subscriber, 1) == [[b"message", b"news", b"hello"]]
    subscriber.close()


def test_bad_bytes_get_an_error_and_the_connection_is_closed(threaded):
    connection = raw_connection(threaded.address)

    connection.sendall(b"*1\r\n$abc\r\n")

    reply = connection.recv(1024)
    assert reply.startswith(b"-ERR Protocol error")
    assert connection.recv(1024) == b""  # then the server hangs up
    connection.close()


def test_quit_closes_the_connection(threaded):
    connection = raw_connection(threaded.address)

    connection.sendall(encode_command("QUIT"))

    assert read_replies(connection, 1) == ["OK"]
    assert connection.recv(1024) == b""
    connection.close()


def test_expired_keys_are_removed_by_the_background_thread(threaded):
    r = client_for(threaded.address)
    r.set("ghost", "v", px=100)

    deadline = time.monotonic() + 3
    while len(threaded.store) and time.monotonic() < deadline:
        time.sleep(0.02)

    assert len(threaded.store) == 0
    assert threaded.store.expired_keys == 1


def test_the_connected_clients_count_goes_down_when_clients_leave(threaded):
    clients = [client_for(threaded.address) for _ in range(3)]
    for r in clients:
        r.ping()
    assert threaded.stats.connected_clients == 3

    for r in clients:
        r.close()
    deadline = time.monotonic() + 3
    while threaded.stats.connected_clients and time.monotonic() < deadline:
        time.sleep(0.02)

    assert threaded.stats.connected_clients == 0


def test_closing_the_server_stops_it_accepting(threaded):
    address = threaded.address
    threaded.close()

    with pytest.raises(OSError):
        socket.create_connection(address, timeout=1)

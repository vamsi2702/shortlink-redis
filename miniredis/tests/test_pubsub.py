import socket
import time
import uuid

import pytest
import redis

from miniredis import server as server_module
from miniredis.commands import ClientState, Context, Replies, execute
from miniredis.pubsub import PubSub
from miniredis.resp import RespError, encode, encode_command
from miniredis.server import encode_reply
from miniredis.store import Store


class Listener:
    """A stand-in client: remembers every message pushed to it."""

    def __init__(self, ident=1, accepts=True):
        self.client = ClientState(id=ident)
        self.received: list[bytes] = []
        self.accepts = accepts
        self.client.push = self._push

    def _push(self, data: bytes) -> bool:
        if not self.accepts:
            return False
        self.received.append(data)
        return True


def message(channel: bytes, payload: bytes) -> bytes:
    return encode([b"message", channel, payload])


# ---- the registry ---------------------------------------------------------------------------


def test_subscribing_counts_the_channels_a_client_is_on():
    pubsub, who = PubSub(), Listener().client

    assert pubsub.subscribe(who, b"a") == 1
    assert pubsub.subscribe(who, b"b") == 2
    assert pubsub.subscribe(who, b"a") == 2  # joining again changes nothing
    assert list(who.subscriptions) == [b"a", b"b"]  # in the order joined


def test_publish_delivers_the_exact_message_and_counts_receivers():
    pubsub = PubSub()
    one, two = Listener(1), Listener(2)
    pubsub.subscribe(one.client, b"room")
    pubsub.subscribe(two.client, b"room")

    assert pubsub.publish(b"room", b"hi") == 2

    assert one.received == two.received == [b"*3\r\n$7\r\nmessage\r\n$4\r\nroom\r\n$2\r\nhi\r\n"]


def test_a_message_only_goes_to_the_subscribers_of_its_channel():
    pubsub = PubSub()
    a, b = Listener(1), Listener(2)
    pubsub.subscribe(a.client, b"a")
    pubsub.subscribe(b.client, b"b")

    assert pubsub.publish(b"a", b"x") == 1

    assert a.received and not b.received


def test_publishing_to_a_channel_nobody_is_on_reaches_nobody():
    assert PubSub().publish(b"empty", b"x") == 0


def test_unsubscribing_returns_what_is_left_and_stops_delivery():
    pubsub = PubSub()
    who = Listener()
    pubsub.subscribe(who.client, b"a")
    pubsub.subscribe(who.client, b"b")

    assert pubsub.unsubscribe(who.client, b"a") == 1
    assert pubsub.unsubscribe(who.client, b"a") == 1  # already gone: harmless
    assert pubsub.unsubscribe(who.client, b"never-joined") == 1
    assert pubsub.publish(b"a", b"x") == 0
    assert pubsub.publish(b"b", b"x") == 1


def test_unsubscribe_all():
    pubsub = PubSub()
    who = Listener()
    for channel in (b"a", b"b", b"c"):
        pubsub.subscribe(who.client, channel)

    pubsub.unsubscribe_all(who.client)

    assert not who.client.subscriptions
    assert pubsub.channel_count == 0
    assert pubsub.publish(b"a", b"x") == 0


def test_counts_of_channels_and_subscribers():
    pubsub = PubSub()
    one, two = Listener(1), Listener(2)
    pubsub.subscribe(one.client, b"a")
    pubsub.subscribe(one.client, b"b")
    pubsub.subscribe(two.client, b"b")

    assert pubsub.channel_count == 2
    assert pubsub.subscriber_count == 2
    pubsub.unsubscribe_all(one.client)
    assert (pubsub.channel_count, pubsub.subscriber_count) == (1, 1)


def test_a_client_that_cannot_be_reached_is_not_counted():
    pubsub = PubSub()
    gone, refuses, fine = Listener(1), Listener(2, accepts=False), Listener(3)
    gone.client.push = None
    for who in (gone, refuses, fine):
        pubsub.subscribe(who.client, b"a")

    assert pubsub.publish(b"a", b"x") == 1


def test_payloads_are_binary_safe():
    pubsub, who = PubSub(), Listener()
    pubsub.subscribe(who.client, b"a")

    pubsub.publish(b"a", b"li\r\nne\x00\xff")

    assert who.received == [message(b"a", b"li\r\nne\x00\xff")]


# ---- the commands ---------------------------------------------------------------------------


@pytest.fixture
def pubsub():
    return PubSub()


def make_client(pubsub, ident=1):
    listener = Listener(ident)
    ctx = Context(store=Store(), client=listener.client, pubsub=pubsub)

    def run(*words):
        return execute(ctx, [w if isinstance(w, bytes) else str(w).encode() for w in words])

    return listener, run


def test_subscribe_confirms_each_channel_with_the_running_count(pubsub):
    _, run = make_client(pubsub)

    reply = run("SUBSCRIBE", "a", "b")

    assert isinstance(reply, Replies)
    assert reply == [[b"subscribe", b"a", 1], [b"subscribe", b"b", 2]]
    assert run("SUBSCRIBE", "a") == [[b"subscribe", b"a", 2]]
    assert run("SUBSCRIBE", "c", "c") == [[b"subscribe", b"c", 3], [b"subscribe", b"c", 3]]


def test_replies_are_encoded_as_separate_messages():
    reply = Replies([[b"subscribe", b"a", 1], [b"subscribe", b"b", 2]])

    assert encode_reply(reply) == (
        b"*3\r\n$9\r\nsubscribe\r\n$1\r\na\r\n:1\r\n*3\r\n$9\r\nsubscribe\r\n$1\r\nb\r\n:2\r\n"
    )


def test_publish_reaches_subscribers_and_returns_how_many(pubsub):
    listener, subscribe_as = make_client(pubsub, 1)
    _, publish_as = make_client(pubsub, 2)
    subscribe_as("SUBSCRIBE", "news")

    assert publish_as("PUBLISH", "news", "hello") == 1
    assert publish_as("PUBLISH", "elsewhere", "hello") == 0

    assert listener.received == [message(b"news", b"hello")]


def test_while_subscribed_most_commands_are_refused(pubsub):
    _, run = make_client(pubsub)
    run("SUBSCRIBE", "a")

    for words in (["GET", "x"], ["SET", "x", "1"], ["PUBLISH", "a", "x"], ["INCR", "n"],
                  ["LPUSH", "l", "x"], ["INFO"], ["TYPE", "x"]):  # fmt: skip
        reply = run(*words)
        assert reply == RespError(
            f"ERR Can't execute '{words[0].lower()}': only (P|S)SUBSCRIBE / (P|S)UNSUBSCRIBE / "
            "PING / QUIT / RESET are allowed in this context"
        ), words


def test_the_unknown_command_and_arity_errors_come_before_the_subscribed_mode_error(pubsub):
    _, run = make_client(pubsub)
    run("SUBSCRIBE", "a")

    assert run("FOO") == RespError("ERR unknown command 'FOO', with args beginning with: ")
    assert run("GET") == RespError("ERR wrong number of arguments for 'get' command")


def test_ping_while_subscribed_answers_with_an_array(pubsub):
    _, run = make_client(pubsub)
    assert encode(run("PING")) == b"+PONG\r\n"  # in normal mode it is the usual simple string
    run("SUBSCRIBE", "a")

    assert run("PING") == [b"pong", b""]
    assert run("PING", "hi") == [b"pong", b"hi"]


def test_quit_is_allowed_while_subscribed(pubsub):
    listener, run = make_client(pubsub)
    run("SUBSCRIBE", "a")

    assert encode(run("QUIT")) == b"+OK\r\n"
    assert listener.client.close_after_reply


def test_unsubscribe_one_channel_all_channels_and_when_on_none(pubsub):
    _, run = make_client(pubsub)
    run("SUBSCRIBE", "x", "y", "z")

    assert run("UNSUBSCRIBE", "y") == [[b"unsubscribe", b"y", 2]]
    assert run("UNSUBSCRIBE", "x", "z") == [[b"unsubscribe", b"x", 1], [b"unsubscribe", b"z", 0]]
    assert run("UNSUBSCRIBE") == [[b"unsubscribe", None, 0]]


def test_unsubscribe_without_names_leaves_every_channel_in_the_order_joined(pubsub):
    """MiniRedis chooses the order joined. (Real Redis's order is unspecified, so a client must
    not depend on it either way.)"""
    _, run = make_client(pubsub)
    run("SUBSCRIBE", "b", "a", "c")

    assert run("UNSUBSCRIBE") == [
        [b"unsubscribe", b"b", 2],
        [b"unsubscribe", b"a", 1],
        [b"unsubscribe", b"c", 0],
    ]


def test_after_the_last_unsubscribe_the_client_is_back_to_normal(pubsub):
    _, run = make_client(pubsub)
    run("SUBSCRIBE", "a")
    run("UNSUBSCRIBE", "a")

    assert encode(run("PING")) == b"+PONG\r\n"
    assert encode(run("SET", "x", "1")) == b"+OK\r\n"


def test_unsubscribing_from_a_channel_not_joined_keeps_the_count(pubsub):
    _, run = make_client(pubsub)
    run("SUBSCRIBE", "a", "b")

    assert run("UNSUBSCRIBE", "nosuch") == [[b"unsubscribe", b"nosuch", 2]]


@pytest.mark.parametrize(
    ("words", "name"),
    [(["SUBSCRIBE"], "subscribe"), (["PUBLISH", "a"], "publish"), (["PUBLISH"], "publish"),
     (["PUBLISH", "a", "b", "c"], "publish")],
)  # fmt: skip
def test_wrong_number_of_arguments(pubsub, words, name):
    _, run = make_client(pubsub)

    assert run(*words) == RespError(f"ERR wrong number of arguments for '{name}' command")


# ---- through the real server ----------------------------------------------------------------


def test_a_subscriber_receives_what_another_client_publishes(connect):
    subscriber, publisher = connect(), connect()
    assert subscriber.call("SUBSCRIBE", "news") == [b"subscribe", b"news", 1]

    assert publisher.call("PUBLISH", "news", "hello") == 1

    assert subscriber.read(timeout=3) == [b"message", b"news", b"hello"]


def test_every_subscriber_gets_the_message(connect):
    publisher = connect()
    subscribers = [connect() for _ in range(25)]
    for s in subscribers:
        s.call("SUBSCRIBE", "room")

    assert publisher.call("PUBLISH", "room", "to all") == 25

    assert [s.read(timeout=3) for s in subscribers] == [[b"message", b"room", b"to all"]] * 25


def test_messages_arrive_in_the_order_they_were_published(connect):
    subscriber, publisher = connect(), connect()
    subscriber.call("SUBSCRIBE", "numbers")
    publisher.send_raw(b"".join(encode_command("PUBLISH", "numbers", i) for i in range(1000)))
    [publisher.read(timeout=10) for _ in range(1000)]

    received = [subscriber.read(timeout=5)[2] for _ in range(1000)]

    assert received == [str(i).encode() for i in range(1000)]


def test_binary_payloads_cross_the_network_intact(connect):
    subscriber, publisher = connect(), connect()
    subscriber.call("SUBSCRIBE", "bin")
    payload = bytes(range(256)) * 100 + b"\r\n+OK\r\n"

    publisher.call("PUBLISH", "bin", payload)

    assert subscriber.read(timeout=3) == [b"message", b"bin", payload]


def test_after_unsubscribing_nothing_more_arrives(connect):
    subscriber, publisher = connect(), connect()
    subscriber.call("SUBSCRIBE", "a", "b")
    subscriber.read(timeout=3)  # the second confirmation
    assert subscriber.call("UNSUBSCRIBE", "a") == [b"unsubscribe", b"a", 1]

    assert publisher.call("PUBLISH", "a", "ignored") == 0
    assert publisher.call("PUBLISH", "b", "heard") == 1

    assert subscriber.read(timeout=3) == [b"message", b"b", b"heard"]


def test_a_client_that_disconnects_stops_being_a_subscriber(connect):
    subscriber, publisher = connect(), connect()
    subscriber.call("SUBSCRIBE", "gone")
    assert publisher.call("PUBLISH", "gone", "x") == 1

    subscriber.close()

    deadline = time.monotonic() + 3
    while publisher.call("PUBLISH", "gone", "x") != 0:
        assert time.monotonic() < deadline, "the closed subscriber was never forgotten"
        time.sleep(0.02)


def test_a_subscriber_can_still_run_ping_and_quit(connect):
    subscriber = connect()
    subscriber.call("SUBSCRIBE", "a")

    assert subscriber.call("PING") == [b"pong", b""]
    assert subscriber.call("GET", "x").message.startswith("ERR Can't execute 'get'")
    assert subscriber.call("QUIT") is not None
    assert subscriber.server_closed_connection()


def test_info_counts_pubsub_activity(connect):
    subscriber, other = connect(), connect()
    subscriber.call("SUBSCRIBE", "a", "b")
    subscriber.read(timeout=3)

    text = other.call("INFO").decode()

    assert "pubsub_clients:1\r\n" in text
    assert "pubsub_channels:2\r\n" in text


def test_a_subscriber_that_never_reads_is_cut_off_instead_of_filling_the_servers_memory(
    server, connect, monkeypatch
):
    monkeypatch.setattr(server_module, "MAX_PUSH_BUFFER", 300_000)
    lazy = socket.socket()
    lazy.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    lazy.connect(server.address)
    lazy.sendall(encode_command("SUBSCRIBE", "firehose"))
    time.sleep(0.2)
    publisher, bystander = connect(), connect()
    big = b"x" * 65_536

    dropped_after = None
    for count in range(1, 3000):
        if publisher.call("PUBLISH", "firehose", big) == 0:
            dropped_after = count
            break
    lazy.close()

    assert dropped_after is not None, "the slow subscriber was never disconnected"
    assert bystander.call("PING") is not None  # everybody else is unaffected


def test_a_slow_subscriber_does_not_hold_up_the_publisher(connect):
    publisher, fast = connect(), connect()
    fast.call("SUBSCRIBE", "a")

    start = time.perf_counter()
    for _ in range(200):
        publisher.call("PUBLISH", "a", "x")

    assert time.perf_counter() - start < 5
    assert [fast.read(timeout=3)[2] for _ in range(200)] == [b"x"] * 200


# ---- redis-py's own Pub/Sub support ---------------------------------------------------------


def test_redis_py_pubsub(server):
    host, port = server.address
    with redis.Redis(host=host, port=port, protocol=2) as r:
        listener = r.pubsub()
        listener.subscribe("news")
        assert listener.get_message(timeout=3)["type"] == "subscribe"

        assert r.publish("news", "hello") == 1

        got = listener.get_message(timeout=3)
        assert got["type"] == "message"
        assert got["channel"] == b"news"
        assert got["data"] == b"hello"
        listener.unsubscribe("news")
        assert listener.get_message(timeout=3)["type"] == "unsubscribe"
        assert r.publish("news", "again") == 0
        listener.close()


def test_redis_py_pubsub_with_several_channels_and_ping(server):
    host, port = server.address
    with redis.Redis(host=host, port=port, protocol=2) as r:
        listener = r.pubsub()
        listener.subscribe("a", "b")
        assert [listener.get_message(timeout=3)["type"] for _ in range(2)] == ["subscribe"] * 2
        listener.ping()
        assert listener.get_message(timeout=3)["type"] == "pong"

        r.publish("b", "to-b")
        r.publish("a", "to-a")

        got = [listener.get_message(timeout=3) for _ in range(2)]
        assert [(m["channel"], m["data"]) for m in got] == [(b"b", b"to-b"), (b"a", b"to-a")]
        listener.close()


# ---- the same behaviour as real Redis -------------------------------------------------------


def run_counted(client, steps, prefix):
    """Each step is (words, how many replies it produces[, are those replies in no set order]);
    returns every reply, flattened. Replies in no set order are summarised so that two servers
    answering in a different order still compare equal."""
    replies = []
    for step in steps:
        words, expected = step[0], step[1]
        unordered = len(step) > 2 and step[2]
        client.send_raw(encode_command(*[w.replace("{p}", prefix) for w in words]))
        got = [client.read(timeout=5) for _ in range(expected)]
        if unordered:
            # real Redis walks a hash table here, so its order depends on the channel names
            got = [("any order", sorted(r[1] for r in got), sorted(r[2] for r in got))]
        replies.extend(got)
    return replies


def normalise(replies, prefix):
    """Replies with the unique prefix taken out of the channel names."""
    def strip(item):
        return item.replace(prefix.encode(), b"") if isinstance(item, bytes) else item

    return [[strip(x) for x in r] if isinstance(r, list) else r for r in replies]


SCENARIOS = {
    "subscribing": [
        (["SUBSCRIBE", "{p}a", "{p}b"], 2),
        (["SUBSCRIBE", "{p}a"], 1),
        (["SUBSCRIBE", "{p}c", "{p}c"], 2),
    ],
    "what is allowed while subscribed": [
        (["SUBSCRIBE", "{p}a"], 1),
        (["PING"], 1),
        (["PING", "hi"], 1),
        (["GET", "{p}x"], 1),
        (["SET", "{p}x", "1"], 1),
        (["PUBLISH", "{p}a", "x"], 1),
        (["INCR", "{p}n"], 1),
        (["FOO"], 1),
        (["GET"], 1),
    ],
    "unsubscribing": [
        (["SUBSCRIBE", "{p}x", "{p}y", "{p}z"], 3),
        (["UNSUBSCRIBE", "{p}y"], 1),
        (["UNSUBSCRIBE", "{p}y"], 1),
        (["UNSUBSCRIBE", "{p}nosuch"], 1),
        (["UNSUBSCRIBE"], 2, True),  # leaves x and z, in an order real Redis does not fix
        (["PING"], 1),
        (["UNSUBSCRIBE"], 1),
        (["SET", "{p}k", "v"], 1),
        (["DEL", "{p}k"], 1),
    ],
    "arguments": [
        (["SUBSCRIBE"], 1),
        (["PUBLISH", "{p}a"], 1),
        (["PUBLISH"], 1),
        (["PUBLISH", "{p}a", "b", "c"], 1),
        (["PUBLISH", "{p}nobody", "x"], 1),
    ],
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_replies_match_real_redis(name, connect, connect_real):
    prefix = f"pubsubtest-{uuid.uuid4().hex[:8]}:"

    ours = normalise(run_counted(connect(), SCENARIOS[name], prefix), prefix)
    theirs = normalise(run_counted(connect_real(), SCENARIOS[name], prefix), prefix)

    assert ours == theirs


def test_delivery_matches_real_redis(connect, connect_real):
    prefix = f"pubsubtest-{uuid.uuid4().hex[:8]}:"
    results = []
    for open_client in (connect, connect_real):
        subscriber, publisher = open_client(), open_client()
        two = open_client()
        subscriber.call("SUBSCRIBE", f"{prefix}room")
        two.call("SUBSCRIBE", f"{prefix}room", f"{prefix}other")
        two.read(timeout=3)
        count = publisher.call("PUBLISH", f"{prefix}room", "hi")
        heard = (subscriber.read(timeout=3), two.read(timeout=3))
        binary = publisher.call("PUBLISH", f"{prefix}other", b"a\r\nb\x00")
        binary_heard = normalise([two.read(timeout=3)], prefix)
        results.append((count, normalise(list(heard), prefix), binary, binary_heard))
        for client in (subscriber, two):
            client.close()

    assert results[0] == results[1]

import random
import time
from collections import deque

import pytest

from miniredis import commands
from miniredis.aof import replay
from miniredis.commands import ClientState, Context, execute
from miniredis.resp import Parser, encode_command
from miniredis.store import Store


class RecordingLog:
    def __init__(self):
        self.commands: list[list[bytes]] = []

    def append(self, words):
        self.commands.append(words)


def dump(store):
    """Everything live in a store, in a form that can be compared."""
    out = {}
    for key, value, at in store.items():
        if isinstance(value, deque):
            value = ("list", list(value))
        elif isinstance(value, dict):
            value = ("hash", list(value.items()))
        else:
            value = ("string", value)
        out[key] = (value, at)
    return out


def replay_into(store, path):
    """Replay a file into a store the way the server does."""
    ctx = Context(store=store, client=ClientState(id=0))
    store.loading = True
    try:
        return replay(path, lambda words: commands.execute(ctx, words))
    finally:
        store.loading = False


def write_log(path, *command_list):
    path.write_bytes(b"".join(encode_command(*words) for words in command_list))


# ---- the parser reports where the last complete message ended -------------------------------


def test_complete_offset_follows_each_message():
    messages = [encode_command("SET", "a", "1"), b"+OK\r\n", b":42\r\n", encode_command("PING")]
    parser = Parser()
    ends = []
    for raw in messages:
        parser.feed(raw)
        list(parser.messages())
        ends.append(parser.complete_offset)

    expected = [sum(len(m) for m in messages[: i + 1]) for i in range(len(messages))]
    assert ends == expected
    assert ends[:3] == [27, 32, 37]


def test_complete_offset_ignores_a_half_received_message():
    parser = Parser()
    parser.feed(encode_command("SET", "a", "1") + b"*3\r\n$3\r\nSET\r\n$1\r\n")
    list(parser.messages())

    assert parser.complete_offset == 27
    assert parser.pending


@pytest.mark.parametrize("piece", [1, 2, 5, 13])
def test_complete_offset_is_the_same_however_the_bytes_arrive(piece):
    stream = b"".join(encode_command("SET", f"k{i}", "x" * i) for i in range(20))
    parser = Parser()
    for i in range(0, len(stream), piece):
        parser.feed(stream[i : i + piece])
        list(parser.messages())

    assert parser.complete_offset == len(stream)


# ---- the loading mode and the expiry hook of the store --------------------------------------


def test_while_loading_nothing_expires_and_past_times_are_only_remembered(clock):
    store = Store(clock=clock)
    store.loading = True
    store.set(b"k", b"v", expire_at=clock.now - 1_000)  # a time that has already passed

    assert store.get(b"k") == b"v"  # not removed while loading
    assert store.purge_expired() == 0
    store.loading = False
    assert store.get(b"k") is None  # but dead as soon as loading is over


def test_on_expire_is_called_once_for_each_key_that_dies(clock):
    store = Store(clock=clock)
    died = []
    store.on_expire = died.append
    store.set(b"lazy", b"v", expire_at=clock.now + 10)
    store.set(b"background", b"v", expire_at=clock.now + 20)
    store.set(b"overwritten", b"v", expire_at=clock.now + 30)
    clock.advance(100)

    assert store.get(b"lazy") is None  # found dead by a read
    store.set(b"overwritten", b"new")  # found dead by a write
    assert store.purge_expired() == 1  # found dead by the background cleaner

    assert died == [b"lazy", b"overwritten", b"background"]


def test_on_expire_is_not_called_for_ordinary_deletes(clock):
    store = Store(clock=clock)
    died = []
    store.on_expire = died.append
    store.set(b"k", b"v", expire_at=clock.now + 1_000)

    store.delete(b"k")
    store.persist(b"k")

    assert died == []


def test_items_lists_live_keys_with_their_expiry(clock):
    store = Store(clock=clock)
    store.set(b"plain", b"1")
    store.set(b"timed", b"2", expire_at=clock.now + 500)
    store.set(b"dead", b"3", expire_at=clock.now - 1)

    assert sorted(store.items()) == [
        (b"plain", b"1", None),
        (b"timed", b"2", clock.now + 500),
    ]


# ---- replaying a file -----------------------------------------------------------------------


def test_replaying_a_missing_file_is_fine(tmp_path, clock):
    store = Store(clock=clock)

    result = replay_into(store, tmp_path / "nothing.aof")

    assert result.commands == 0
    assert len(store) == 0


def test_replay_rebuilds_the_data(tmp_path, clock):
    path = tmp_path / "x.aof"
    write_log(
        path,
        ["SET", "a", "1"],
        ["SET", "b", "2", "PXAT", str(clock.now + 5_000)],
        ["LPUSH", "l", "x", "y"],
        ["HSET", "h", "f", "v"],
        ["SET", "a", "3", "KEEPTTL"],
        ["DEL", "b"],
        ["RPOP", "l"],
    )
    store = Store(clock=clock)

    result = replay_into(store, path)

    assert result.commands == 7
    assert result.errors == 0
    assert result.discarded_bytes == 0
    assert dump(store) == {
        b"a": (("string", b"3"), None),
        b"l": (("list", [b"y"]), None),
        b"h": (("hash", [(b"f", b"v")]), None),
    }


def test_replay_gives_the_same_result_however_the_file_is_read(tmp_path, clock):
    path = tmp_path / "x.aof"
    write_log(path, *[["SET", f"k{i}", "v" * i] for i in range(200)])
    reference = Store(clock=clock)
    replay_into(reference, path)

    for chunk_size in (1, 3, 7, 100, 1 << 20):
        store = Store(clock=clock)
        ctx = Context(store=store, client=ClientState(id=0))
        replay(path, lambda words, ctx=ctx: execute(ctx, words), chunk_size=chunk_size)
        assert dump(store) == dump(reference)


def test_a_command_that_fails_when_replayed_is_counted_and_replay_goes_on(tmp_path, clock):
    path = tmp_path / "x.aof"
    write_log(path, ["SET", "word", "hello"], ["INCR", "word"], ["SET", "after", "1"])
    store = Store(clock=clock)

    result = replay_into(store, path)

    assert (result.commands, result.errors) == (3, 1)
    assert store.get(b"after") == b"1"


def test_replay_does_not_write_to_the_log_again(tmp_path, clock):
    path = tmp_path / "x.aof"
    write_log(path, ["SET", "a", "1"], ["LPUSH", "l", "x"])
    before = path.read_bytes()

    replay_into(Store(clock=clock), path)

    assert path.read_bytes() == before


def test_a_key_that_was_alive_when_its_command_ran_is_alive_while_replaying(tmp_path, clock):
    """EXPIRE with a time that has since passed must not delete the key before the next command
    (which ran while the key was still alive) touches it."""
    path = tmp_path / "x.aof"
    long_ago = clock.now - 60_000
    write_log(
        path,
        ["LPUSH", "l", "a"],
        ["PEXPIREAT", "l", str(long_ago)],
        ["LPUSH", "l", "b"],  # the list was alive then: this adds to it
    )
    store = Store(clock=clock)

    replay_into(store, path)

    assert store.get(b"l") is None  # and now, with loading over, the old time has passed


def test_a_delete_written_at_expiry_makes_the_next_command_start_fresh(tmp_path, clock):
    path = tmp_path / "x.aof"
    write_log(
        path,
        ["LPUSH", "l", "a"],
        ["PEXPIREAT", "l", str(clock.now - 60_000)],
        ["DEL", "l"],  # written when the list died of old age
        ["LPUSH", "l", "b"],  # a brand new list
    )
    store = Store(clock=clock)

    replay_into(store, path)

    assert dump(store) == {b"l": (("list", [b"b"]), None)}


# ---- the big one: a replayed log gives exactly the data that was there ----------------------


KEYS = [b"a", b"b", b"c", b"d"]


def random_command(rng):
    key = rng.choice(KEYS)
    kind = rng.choice(
        ["set", "set_ex", "set_flag", "get", "del", "incr", "incrby", "expire", "pexpire",
         "expireat", "persist", "lpush", "rpop", "hset", "ttl", "type"]
    )  # fmt: skip
    number = str(rng.randrange(0, 12))
    if kind == "set":
        return [b"SET", key, rng.choice([b"1", b"7", b"text", b""])]
    if kind == "set_ex":
        option = rng.choice([b"EX", b"PX", b"EXAT", b"PXAT"])
        return [b"SET", key, b"5", option, str(rng.randrange(1, 9)).encode()]
    if kind == "set_flag":
        return [b"SET", key, b"9", rng.choice([b"NX", b"XX", b"KEEPTTL"])]
    if kind == "get":
        return [b"GET", key]
    if kind == "del":
        return [b"DEL", key, rng.choice(KEYS)]
    if kind == "incr":
        return [b"INCR", key]
    if kind == "incrby":
        return [b"INCRBY", key, str(rng.randrange(-5, 6)).encode()]
    if kind == "expire":
        option = rng.choice([[], [b"GT"], [b"NX"]])
        return [b"EXPIRE", key, str(rng.randrange(-1, 6)).encode(), *option]
    if kind == "pexpire":
        return [b"PEXPIRE", key, str(rng.randrange(0, 4000)).encode()]
    if kind == "expireat":
        return [b"PEXPIREAT", key, b"1"]  # the distant past: deletes the key
    if kind == "persist":
        return [b"PERSIST", key]
    if kind == "lpush":
        return [b"LPUSH", key, number.encode(), b"x"]
    if kind == "rpop":
        return [b"RPOP", key, *rng.choice([[], [b"2"], [b"100"]])]
    if kind == "hset":
        return [b"HSET", key, rng.choice([b"f", b"g"]), number.encode()]
    if kind == "ttl":
        return [b"TTL", key]
    return [b"TYPE", key]


def random_session(seed, clock, steps=250):
    """Run random commands (and let time pass) on a store that logs like the real server."""
    rng = random.Random(seed)
    log = RecordingLog()
    store = Store(clock=clock)
    store.on_expire = lambda key: log.append([b"DEL", key])
    ctx = Context(store=store, client=ClientState(id=1), aof=log)
    for _ in range(steps):
        if rng.random() < 0.4:
            clock.advance(rng.choice([0, 100, 700, 1500, 3000, 6000]))
        if rng.random() < 0.1:
            store.purge_expired()  # the background cleaner
        execute(ctx, random_command(rng))
    return store, log.commands


@pytest.mark.parametrize("seed", range(300))
def test_replaying_the_log_of_a_random_session_rebuilds_the_same_data(seed, clock):
    original, log = random_session(seed, clock)
    replayed = Store(clock=clock)  # the same moment in time: the end of the session
    context = Context(store=replayed, client=ClientState(id=0))

    replayed.loading = True
    for words in log:
        execute(context, words)
    replayed.loading = False

    assert dump(replayed) == dump(original)


@pytest.mark.parametrize("seed", range(100))
def test_and_it_stays_the_same_as_time_goes_on(seed, clock):
    original, log = random_session(seed, clock)
    replayed = Store(clock=clock)
    context = Context(store=replayed, client=ClientState(id=0))
    replayed.loading = True
    for words in log:
        execute(context, words)
    replayed.loading = False

    for _ in range(5):
        clock.advance(2_000)
        assert dump(replayed) == dump(original)


def test_the_random_sessions_really_exercise_expiry_and_every_data_type(clock):
    kinds = set()
    expiries = 0
    for seed in range(40):
        store, log = random_session(seed, clock)
        kinds.update(type(value).__name__ for _, value, _ in store.items())
        expiries += sum(1 for words in log if words[0] == b"DEL" and len(words) == 2)

    assert {"bytes", "deque", "dict"} <= kinds
    assert expiries > 20


# ---- through the real server, restarting it -------------------------------------------------


def test_data_survives_a_restart(aof_path, make_server, make_client):
    first = make_server(aof_path=aof_path)
    c = make_client(first.address)
    c.call("SET", "name", "vamsi")
    c.call("INCRBY", "visits", 41)
    c.call("INCR", "visits")
    c.call("LPUSH", "queue", "one", "two", "three")
    c.call("HSET", "stats", "views", 10, "last", "now")
    c.call("SET", "gone", "x")
    c.call("DEL", "gone")
    first.stop()

    second = make_server(aof_path=aof_path)
    c = make_client(second.address)

    assert c.call("GET", "name") == b"vamsi"
    assert c.call("GET", "visits") == b"42"
    assert c.call("LRANGE", "queue", 0, -1) == [b"three", b"two", b"one"]
    assert c.call("HGETALL", "stats") == [b"views", b"10", b"last", b"now"]
    assert c.call("EXISTS", "gone") == 0


def test_a_queue_keeps_its_order_and_what_was_already_taken_stays_taken(
    aof_path, make_server, make_client
):
    first = make_server(aof_path=aof_path)
    c = make_client(first.address)
    for item in "abcdef":
        c.call("LPUSH", "q", item)
    assert c.call("RPOP", "q", 2) == [b"a", b"b"]
    first.stop()

    c = make_client(make_server(aof_path=aof_path).address)

    assert c.call("RPOP", "q", 10) == [b"c", b"d", b"e", b"f"]


def test_a_key_whose_time_ran_out_while_the_server_was_off_is_gone(
    aof_path, make_server, make_client
):
    first = make_server(aof_path=aof_path)
    c = make_client(first.address)
    c.call("SET", "short", "v", "PX", 300)
    c.call("SET", "long", "v", "EX", 100)
    c.call("SET", "forever", "v")
    first.stop()
    time.sleep(0.45)

    c = make_client(make_server(aof_path=aof_path).address)

    assert c.call("GET", "short") is None
    assert c.call("GET", "long") == b"v"
    assert 90 <= c.call("TTL", "long") <= 100  # the time keeps counting down while off
    assert c.call("GET", "forever") == b"v"


def test_restarting_twice_keeps_everything_and_changes_nothing_in_the_file(
    aof_path, make_server, make_client
):
    first = make_server(aof_path=aof_path)
    make_client(first.address).call("SET", "a", "1")
    first.stop()
    content = aof_path.read_bytes()

    for _ in range(2):
        again = make_server(aof_path=aof_path)
        assert make_client(again.address).call("GET", "a") == b"1"
        again.stop()
        assert aof_path.read_bytes() == content  # replaying logs nothing


def test_changes_made_after_a_restart_survive_the_next_restart(aof_path, make_server, make_client):
    first = make_server(aof_path=aof_path)
    make_client(first.address).call("SET", "before", "1")
    first.stop()
    second = make_server(aof_path=aof_path)
    c = make_client(second.address)
    c.call("SET", "after", "2")
    c.call("INCR", "before")
    second.stop()

    c = make_client(make_server(aof_path=aof_path).address)

    assert c.call("GET", "before") == b"2"
    assert c.call("GET", "after") == b"2"


def test_a_starting_server_with_no_file_starts_empty_and_creates_it(
    aof_path, make_server, make_client
):
    assert not aof_path.exists()

    server = make_server(aof_path=aof_path)

    assert make_client(server.address).call("PING") is not None
    assert aof_path.exists()
    assert server.redis.aof_load.commands == 0


def test_the_server_reports_what_it_replayed(aof_path, make_server):
    write_log(
        aof_path,
        ["SET", "a", "1"],
        ["SET", "b", "2"],
        ["INCR", "a"],
        ["INCR", "fresh"],
        ["SET", "t", "x"],
        ["INCR", "t"],  # the text "x" is not a number, so this one fails when replayed
    )

    server = make_server(aof_path=aof_path)

    assert server.redis.aof_load.commands == 6
    assert server.redis.aof_load.errors == 1  # INCR of the text value
    assert not server.store.loading
    assert server.store.on_expire is not None


def test_a_key_that_dies_while_running_is_deleted_in_the_log_too(
    aof_path, make_server, make_client
):
    server = make_server(aof_path=aof_path)
    c = make_client(server.address)
    c.call("SET", "k", "v", "PX", 100)
    deadline = time.monotonic() + 3
    while len(server.store) and time.monotonic() < deadline:
        time.sleep(0.02)  # the background cleaner kills it; nobody reads it

    time.sleep(0.1)
    log = aof_path.read_bytes()

    assert log.startswith(b"*5\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\nv\r\n$4\r\nPXAT")
    assert log.endswith(encode_command("DEL", "k"))


def test_a_key_found_dead_by_a_command_is_deleted_in_the_log_too(
    aof_path, make_server, make_client
):
    server = make_server(aof_path=aof_path)
    c = make_client(server.address)
    c.call("LPUSH", "l", "old")
    c.call("PEXPIRE", "l", 100)
    time.sleep(0.3)  # the list is dead now; whoever finds it first writes the DEL

    assert c.call("LPUSH", "l", "new") == 1  # a fresh one-item list, not "old" plus "new"

    parser = Parser()
    parser.feed(aof_path.read_bytes())
    names = [message[0] for message in parser.messages()]
    assert names == [b"LPUSH", b"PEXPIREAT", b"DEL", b"LPUSH"]


def test_a_big_log_is_replayed(aof_path, make_server, make_client):
    first = make_server(aof_path=aof_path)
    c = make_client(first.address)
    count = 5_000
    c.send_raw(b"".join(encode_command("SET", f"k{i}", f"value-{i}") for i in range(count)))
    [c.read(timeout=20) for _ in range(count)]
    first.stop()

    second = make_server(aof_path=aof_path)
    c = make_client(second.address)

    assert len(second.store) == count
    assert c.call("GET", "k4321") == b"value-4321"

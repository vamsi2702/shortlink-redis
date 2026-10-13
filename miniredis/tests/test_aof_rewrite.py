import os
import time

import pytest
from helpers import dump, random_session, replay_into

from miniredis.aof import ITEMS_PER_COMMAND, AppendOnlyFile, rewrite_commands
from miniredis.commands import ClientState, Context, execute
from miniredis.resp import RespError, encode_command
from miniredis.server import MiniRedisServer
from miniredis.store import Store


def as_text(commands):
    return [[word.decode() for word in command] for command in commands]


def build(clock, *commands):
    """A store after running the commands, with the log those commands wrote."""
    store = Store(clock=clock)
    log = []

    class Recorder:
        def append(self, words):
            log.append(words)

    ctx = Context(store=store, client=ClientState(id=1), aof=Recorder())
    for words in commands:
        execute(ctx, [w if isinstance(w, bytes) else str(w).encode() for w in words])
    return store, log


# ---- turning data back into commands --------------------------------------------------------


def test_a_string_is_one_set(clock):
    store, _ = build(clock, ["SET", "a", "1"])

    assert as_text(rewrite_commands(store.items())) == [["SET", "a", "1"]]


def test_a_string_with_an_expiry_keeps_its_exact_time(clock):
    store, _ = build(clock, ["SET", "a", "1", "PX", 5000])

    assert as_text(rewrite_commands(store.items())) == [
        ["SET", "a", "1", "PXAT", str(clock.now + 5000)]
    ]


def test_a_list_is_written_so_it_comes_back_in_the_same_order(clock):
    store, _ = build(clock, ["LPUSH", "l", "a", "b", "c"], ["LPUSH", "l", "d"])
    # the list reads d c b a (d is the front); LPUSH puts each value in front of the one before

    assert as_text(rewrite_commands(store.items())) == [["LPUSH", "l", "a", "b", "c", "d"]]


def test_a_list_and_a_hash_with_an_expiry_get_a_pexpireat(clock):
    store, _ = build(
        clock,
        ["LPUSH", "l", "x"],
        ["HSET", "h", "f", "v"],
        ["EXPIRE", "l", 10],
        ["PEXPIRE", "h", 99],
    )

    assert as_text(rewrite_commands(store.items())) == [
        ["LPUSH", "l", "x"],
        ["PEXPIREAT", "l", str(clock.now + 10_000)],
        ["HSET", "h", "f", "v"],
        ["PEXPIREAT", "h", str(clock.now + 99)],
    ]


def test_a_big_list_is_split_into_several_commands(clock):
    count = ITEMS_PER_COMMAND * 3 + 8
    store, _ = build(clock, ["LPUSH", "l", *[str(i) for i in range(count)]])

    commands = list(rewrite_commands(store.items()))

    assert [len(c) - 2 for c in commands] == [ITEMS_PER_COMMAND] * 3 + [8]
    fresh = Store(clock=clock)
    ctx = Context(store=fresh, client=ClientState(id=0))
    for words in commands:
        execute(ctx, words)
    assert list(fresh.get(b"l")) == list(store.get(b"l"))


def test_a_big_hash_is_split_and_keeps_its_field_order(clock):
    count = ITEMS_PER_COMMAND * 2 + 5
    store, _ = build(clock, *[["HSET", "h", f"f{i}", i] for i in range(count)])

    commands = list(rewrite_commands(store.items()))

    assert len(commands) == 3
    fresh = Store(clock=clock)
    ctx = Context(store=fresh, client=ClientState(id=0))
    for words in commands:
        execute(ctx, words)
    assert list(fresh.get(b"h").items()) == list(store.get(b"h").items())


def test_dead_keys_are_left_out(clock):
    store, _ = build(clock, ["SET", "alive", "1"], ["SET", "dead", "1", "PX", 10])
    clock.advance(100)

    assert as_text(rewrite_commands(store.items())) == [["SET", "alive", "1"]]


def test_binary_data_survives(clock):
    value = b"line1\r\nline2\x00\xff"
    store, _ = build(clock, [b"SET", b"k\r\n", value])

    assert list(rewrite_commands(store.items())) == [[b"SET", b"k\r\n", value]]


def test_something_that_is_not_string_list_or_hash_is_refused():
    with pytest.raises(TypeError):
        list(rewrite_commands([(b"k", 12345, None)]))


# ---- rewriting the file ---------------------------------------------------------------------


@pytest.fixture
def aof(aof_path):
    log = AppendOnlyFile(aof_path)
    log.open()
    yield log
    log.close()


def log_changes(aof, log):
    for words in log:
        aof.append(words)
    aof.commit()


def test_many_changes_to_one_key_become_one_command(aof, aof_path, clock):
    store, log = build(clock, *[["INCR", "counter"] for _ in range(1000)])
    log_changes(aof, log)
    before = aof.size

    aof.rewrite(store.items())

    assert before > 10_000
    assert aof_path.read_bytes() == encode_command("SET", "counter", "1000")
    assert aof.size == len(aof_path.read_bytes())


def test_deleted_data_disappears_from_the_file(aof, aof_path, clock):
    store, log = build(clock, ["SET", "big", "x" * 10_000], ["DEL", "big"], ["SET", "keep", "1"])
    log_changes(aof, log)

    aof.rewrite(store.items())

    assert aof_path.read_bytes() == encode_command("SET", "keep", "1")


def test_the_file_keeps_working_after_a_rewrite(aof, aof_path, clock):
    store, log = build(clock, ["SET", "a", "1"], ["SET", "a", "2"])
    log_changes(aof, log)
    aof.rewrite(store.items())

    aof.append([b"SET", b"b", b"3"])
    aof.commit()

    expected = encode_command("SET", "a", "2") + encode_command("SET", "b", "3")
    assert aof_path.read_bytes() == expected
    assert aof.size == len(expected)


def test_the_temporary_file_is_gone_afterwards(aof, aof_path, clock):
    store, _ = build(clock, ["SET", "a", "1"])

    aof.rewrite(store.items())

    assert not aof_path.with_name(aof_path.name + ".rewrite").exists()


def test_bookkeeping_after_a_rewrite(aof, clock):
    store, log = build(clock, *[["INCR", "n"] for _ in range(200)])
    log_changes(aof, log)
    assert aof.rewrites == 0

    aof.rewrite(store.items())

    assert aof.rewrites == 1
    assert aof.base_size == aof.size
    assert aof.last_rewrite_seconds >= 0


def test_changes_logged_but_not_yet_written_are_not_applied_twice(aof, aof_path, clock):
    store, log = build(clock, ["LPUSH", "l", "x"], ["INCR", "n"])
    for words in log:
        aof.append(words)  # remembered, but not committed: the snapshot already includes them

    aof.rewrite(store.items())
    aof.commit()

    fresh = Store(clock=clock)
    replay_into(fresh, aof_path)
    assert list(fresh.get(b"l")) == [b"x"]  # one item, not two
    assert fresh.get(b"n") == b"1"
    assert not aof.pending


def test_rewriting_an_empty_store_leaves_an_empty_file(aof, aof_path, clock):
    aof.append([b"SET", b"a", b"1"])
    aof.commit()

    aof.rewrite(Store(clock=clock).items())

    assert aof_path.read_bytes() == b""


def test_a_failed_swap_leaves_the_old_log_untouched_and_usable(aof, aof_path, clock, monkeypatch):
    store, log = build(clock, ["SET", "a", "1"], ["SET", "a", "2"])
    log_changes(aof, log)
    old = aof_path.read_bytes()
    aof.append([b"SET", b"later", b"x"])  # not yet written

    def refuse(source, destination):
        raise PermissionError("the file is in use")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(PermissionError):
        aof.rewrite(store.items())
    monkeypatch.undo()

    assert aof_path.read_bytes() == old
    assert not aof_path.with_name(aof_path.name + ".rewrite").exists()
    assert aof.pending  # what had not been written is still waiting
    aof.commit()  # and the file is still open for appending
    assert aof_path.read_bytes() == old + encode_command("SET", "later", "x")


def test_an_error_while_building_the_new_log_leaves_the_old_one_untouched(aof, aof_path, clock):
    store, log = build(clock, ["SET", "a", "1"])
    log_changes(aof, log)
    old = aof_path.read_bytes()

    def broken_items():
        yield b"a", b"1", None
        raise RuntimeError("something went wrong half way")

    with pytest.raises(RuntimeError):
        aof.rewrite(broken_items())

    assert aof_path.read_bytes() == old
    assert not aof_path.with_name(aof_path.name + ".rewrite").exists()
    assert aof.rewrites == 0


def test_a_leftover_temporary_file_from_a_crash_is_removed_at_startup(aof_path):
    aof_path.write_bytes(encode_command("SET", "real", "1"))
    leftover = aof_path.with_name(aof_path.name + ".rewrite")
    leftover.write_bytes(b"half of a rewrite that never finished")

    log = AppendOnlyFile(aof_path)
    log.open()

    assert not leftover.exists()
    assert aof_path.read_bytes() == encode_command("SET", "real", "1")
    log.close()


# ---- the big one: the rewritten log rebuilds exactly the same data --------------------------


@pytest.mark.parametrize("seed", range(300))
def test_the_rewritten_log_of_a_random_session_rebuilds_the_same_data(seed, clock, tmp_path):
    original, log = random_session(seed, clock)
    path = tmp_path / "rewritten.aof"
    file = AppendOnlyFile(path)
    file.open()
    file.rewrite(original.items())
    file.close()

    rebuilt = Store(clock=clock)
    result = replay_into(rebuilt, path)

    assert result.errors == 0
    assert result.discarded_bytes == 0
    assert dump(rebuilt) == dump(original)


@pytest.mark.parametrize("seed", range(60))
def test_rewriting_twice_changes_nothing(seed, clock, tmp_path):
    original, _ = random_session(seed, clock)
    path = tmp_path / "x.aof"
    file = AppendOnlyFile(path)
    file.open()
    file.rewrite(original.items())
    first = path.read_bytes()
    rebuilt = Store(clock=clock)
    replay_into(rebuilt, path)

    file.rewrite(rebuilt.items())
    file.close()

    assert path.read_bytes() == first


def test_the_rewritten_log_is_much_shorter_than_the_original_one(clock, tmp_path):
    original, log = random_session(1, clock, steps=2000)
    long_log = sum(len(encode_command(*words)) for words in log)
    path = tmp_path / "short.aof"
    file = AppendOnlyFile(path)
    file.open()

    file.rewrite(original.items())
    file.close()

    assert path.stat().st_size < long_log / 5


# ---- deciding when to rewrite by itself -----------------------------------------------------


@pytest.mark.parametrize(
    ("size", "base", "minimum", "percentage", "expected"),
    [
        (100, 100, 64, 100, False),  # has not grown
        (150, 100, 64, 100, False),  # grown 50%, wants 100%
        (200, 100, 64, 100, True),  # exactly doubled
        (5000, 100, 64, 100, True),
        (200, 100, 1000, 100, False),  # grown enough, but smaller than the minimum size
        (1000, 100, 1000, 100, True),  # at the minimum
        (5000, 100, 64, 0, False),  # percentage 0 turns it off
        (200, 0, 64, 100, True),  # a log that started empty
        (63, 0, 64, 100, False),
        (300, 100, 64, 200, True),
        (299, 100, 64, 200, False),
    ],
)
def test_when_the_server_decides_to_compact(
    size, base, minimum, percentage, expected, aof_path
):
    server = MiniRedisServer(
        aof_path=aof_path, auto_rewrite_min_size=minimum, auto_rewrite_percentage=percentage
    )
    server.aof.size, server.aof.base_size = size, base

    assert server.should_rewrite() is expected


# ---- through the real server ----------------------------------------------------------------


def test_bgrewriteaof_needs_a_log(client):
    reply = client.call("BGREWRITEAOF")

    assert reply == RespError("ERR the append-only file is not enabled")


def test_bgrewriteaof_takes_no_arguments(aof_client):
    reply = aof_client.call("BGREWRITEAOF", "now")

    assert reply == RespError("ERR wrong number of arguments for 'bgrewriteaof' command")


def test_bgrewriteaof_compacts_the_log_and_nothing_is_lost(
    aof_path, make_server, make_client
):
    first = make_server(aof_path=aof_path)
    c = make_client(first.address)
    for i in range(1500):
        c.call("INCR", "visits")
        c.call("SET", "temp", f"value-{i}")
    c.call("LPUSH", "queue", "a", "b", "c")
    c.call("HSET", "stats", "views", 10)
    c.call("SET", "ttl", "v", "EX", 1000)
    before = aof_path.stat().st_size

    reply = c.call("BGREWRITEAOF")

    assert reply == "Append only file rewriting done"
    assert aof_path.stat().st_size < before / 20
    persistence = parse_info(c.call("INFO", "persistence"))
    assert persistence["aof_rewrites"] == "1"
    assert persistence["aof_current_size"] == str(aof_path.stat().st_size)
    assert persistence["aof_base_size"] == persistence["aof_current_size"]
    # the data is all still there, and new changes keep being logged after the rewrite
    c.call("INCR", "visits")
    first.stop()

    again = make_client(make_server(aof_path=aof_path).address)
    assert again.call("GET", "visits") == b"1501"
    assert again.call("GET", "temp") == b"value-1499"
    assert again.call("LRANGE", "queue", 0, -1) == [b"c", b"b", b"a"]
    assert again.call("HGETALL", "stats") == [b"views", b"10"]
    assert 990 <= again.call("TTL", "ttl") <= 1000


def test_the_server_compacts_the_log_by_itself_when_it_has_grown_a_lot(
    aof_path, make_server, make_client
):
    server = make_server(
        aof_path=aof_path, auto_rewrite_min_size=3000, auto_rewrite_percentage=100
    )
    c = make_client(server.address)
    for i in range(300):
        c.call("SET", "same-key", f"value-{i:04d}-" + "x" * 20)  # 300 changes, one key

    deadline = time.monotonic() + 5
    while server.redis.aof.rewrites == 0 and time.monotonic() < deadline:
        time.sleep(0.1)

    assert server.redis.aof.rewrites >= 1
    assert aof_path.stat().st_size < 3000
    assert c.call("GET", "same-key").startswith(b"value-")


def test_with_a_percentage_of_zero_the_server_never_compacts_by_itself(
    aof_path, make_server, make_client
):
    server = make_server(aof_path=aof_path, auto_rewrite_min_size=100, auto_rewrite_percentage=0)
    c = make_client(server.address)
    for i in range(200):
        c.call("SET", "same-key", f"value-{i}")

    time.sleep(2.5)

    assert server.redis.aof.rewrites == 0


def test_a_rewrite_with_a_hundred_thousand_keys_finishes_in_reasonable_time(clock, tmp_path):
    store = Store(clock=clock)
    for i in range(100_000):
        store.set(f"key:{i}".encode(), b"value-%d" % i)
    path = tmp_path / "big.aof"
    file = AppendOnlyFile(path)
    file.open()

    start = time.perf_counter()
    file.rewrite(store.items())
    elapsed = time.perf_counter() - start
    file.close()

    assert elapsed < 10
    rebuilt = Store(clock=clock)
    assert replay_into(rebuilt, path).commands == 100_000
    assert len(rebuilt) == 100_000


def parse_info(text: bytes) -> dict[str, str]:
    lines = [line for line in text.decode().split("\r\n") if line and not line.startswith("#")]
    return dict(line.split(":", 1) for line in lines)

import os
import random
import socket
import subprocess
import sys
import time

import pytest

from miniredis import commands
from miniredis.aof import AppendOnlyFile, replay
from miniredis.commands import ClientState, Context, execute
from miniredis.resp import encode_command
from miniredis.store import Store

SAMPLE = [
    ["SET", "name", "vamsi"],
    ["SET", "counter", "1", "KEEPTTL"],
    ["LPUSH", "queue", "one", "two", "three"],
    ["HSET", "stats", "views", "10", "last", "now"],
    ["DEL", "name"],
    ["SET", "bin", "line1\r\nline2\x00\xff"],
    ["RPOP", "queue", "2"],
    ["SET", "later", "x", "PXAT", "99999999999999"],
    ["LPUSH", "queue", "four"],
    ["PERSIST", "later"],
]
ENCODED = [encode_command(*words) for words in SAMPLE]
LOG = b"".join(ENCODED)
ENDS = [sum(len(e) for e in ENCODED[: i + 1]) for i in range(len(ENCODED))]  # command end offsets


def state_after(count, clock):
    """The data after the first `count` commands of SAMPLE, worked out the direct way."""
    store = Store(clock=clock)
    ctx = Context(store=store, client=ClientState(id=0))
    store.loading = True
    for words in SAMPLE[:count]:
        execute(ctx, [w.encode() for w in words])  # the same encoding encode_command uses
    store.loading = False
    return dump(store)


def dump(store):
    from collections import deque

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


def recover(path, clock, chunk_size=1 << 20):
    store = Store(clock=clock)
    ctx = Context(store=store, client=ClientState(id=0))
    store.loading = True
    try:
        result = replay(path, lambda words: commands.execute(ctx, words), chunk_size)
    finally:
        store.loading = False
    return store, result


def discarded(path):
    saved = path.with_name(path.name + ".discarded")
    return saved.read_bytes() if saved.exists() else b""


# ---- a log cut off at every possible byte ---------------------------------------------------


def test_the_sample_log_is_what_we_think_it_is(clock):
    assert len(LOG) == ENDS[-1]
    assert state_after(len(SAMPLE), clock)[b"queue"][0] == ("list", [b"four", b"three"])


@pytest.mark.parametrize("cut", range(len(LOG) + 1))
def test_a_log_cut_at_any_byte_recovers_everything_before_the_cut(cut, tmp_path, clock):
    path = tmp_path / "x.aof"
    path.write_bytes(LOG[:cut])
    complete = sum(1 for end in ENDS if end <= cut)  # commands that are fully there
    good = ENDS[complete - 1] if complete else 0

    store, result = recover(path, clock)

    assert dump(store) == state_after(complete, clock)
    assert result.commands == complete
    assert result.valid_bytes == good
    assert result.discarded_bytes == cut - good
    assert path.read_bytes() == LOG[:good]  # the file now ends on a command boundary
    assert discarded(path) == LOG[good:cut]  # and what was removed is kept
    assert (result.reason is None) == (cut == good)


@pytest.mark.parametrize("cut", range(0, len(LOG) + 1, 7))
@pytest.mark.parametrize("chunk_size", [1, 5, 64])
def test_recovery_is_the_same_whatever_size_the_file_is_read_in(cut, chunk_size, tmp_path, clock):
    path = tmp_path / "x.aof"
    path.write_bytes(LOG[:cut])
    complete = sum(1 for end in ENDS if end <= cut)

    store, result = recover(path, clock, chunk_size=chunk_size)

    assert dump(store) == state_after(complete, clock)
    assert result.commands == complete


@pytest.mark.parametrize("cut", [1, 5, 40, 200, len(LOG) - 1])
def test_after_a_recovery_the_next_start_is_clean_and_new_writes_follow_good_data(
    cut, tmp_path, clock
):
    path = tmp_path / "x.aof"
    path.write_bytes(LOG[:cut])
    complete = sum(1 for end in ENDS if end <= cut)
    recover(path, clock)

    _, again = recover(path, clock)  # nothing left to throw away
    assert again.discarded_bytes == 0
    assert again.commands == complete

    log = AppendOnlyFile(path)
    log.open()
    log.append([b"SET", b"after", b"recovery"])
    log.commit()
    log.close()
    store, final = recover(path, clock)
    assert final.discarded_bytes == 0
    assert store.get(b"after") == b"recovery"
    assert final.commands == complete + 1


# ---- other kinds of damage ------------------------------------------------------------------


def test_garbage_at_the_end_is_cut_off(tmp_path, clock):
    path = tmp_path / "x.aof"
    path.write_bytes(LOG + b"this is not redis\r\n")

    store, result = recover(path, clock)

    assert result.commands == len(SAMPLE)
    assert result.discarded_bytes == len(b"this is not redis\r\n")
    assert "invalid data" in result.reason
    assert path.read_bytes() == LOG
    assert dump(store) == state_after(len(SAMPLE), clock)


def test_a_file_extended_with_zero_bytes_is_cut_back(tmp_path, clock):
    """After a crash some filesystems leave the file longer, filled with zeros."""
    path = tmp_path / "x.aof"
    path.write_bytes(LOG + b"\x00" * 4096)

    store, result = recover(path, clock)

    assert result.commands == len(SAMPLE)
    assert result.discarded_bytes == 4096
    assert path.read_bytes() == LOG
    assert discarded(path) == b"\x00" * 4096


def test_a_damaged_command_in_the_middle_ends_the_trusted_part_there(tmp_path, clock):
    """Once one command is unreadable the rest cannot be trusted to line up, so it is kept aside
    and not replayed, even though the commands after it look fine."""
    damaged = bytearray(LOG)
    start_of_fifth = ENDS[3]
    damaged[start_of_fifth] = ord("?")  # the "*" that starts command number 5
    path = tmp_path / "x.aof"
    path.write_bytes(bytes(damaged))

    store, result = recover(path, clock)

    assert result.commands == 4
    assert dump(store) == state_after(4, clock)
    assert path.read_bytes() == LOG[:start_of_fifth]
    assert discarded(path) == bytes(damaged[start_of_fifth:])  # nothing is lost: it is all kept
    assert result.discarded_bytes == len(LOG) - start_of_fifth


@pytest.mark.parametrize(
    "damage",
    [
        lambda log, at: log[: at + 1] + b"x" + log[at + 2 :],  # array length becomes a letter
        lambda log, at: log[: at + 4] + b"Z" + log[at + 5 :],  # string length becomes a letter
        lambda log, at: log[: at + 2] + b"\n\n" + log[at + 4 :],  # the CRLF after a length breaks
    ],
)
def test_a_corrupted_last_entry_is_dropped_and_everything_before_it_is_kept(
    damage, tmp_path, clock
):
    start_of_last = ENDS[-2]
    path = tmp_path / "x.aof"
    path.write_bytes(damage(LOG, start_of_last))

    store, result = recover(path, clock)

    assert result.commands == len(SAMPLE) - 1
    assert dump(store) == state_after(len(SAMPLE) - 1, clock)
    assert path.read_bytes() == LOG[:start_of_last]


def test_changed_data_bytes_cannot_be_detected_because_the_log_has_no_checksums(tmp_path, clock):
    """An honest limit: if a disk flips a byte INSIDE a value, the command is still valid RESP.
    Only structural damage is noticed. (Real Redis has the same limit.)"""
    changed = LOG.replace(b"vamsi", b"vamsX")
    path = tmp_path / "x.aof"
    path.write_bytes(changed)

    store, result = recover(path, clock)

    assert result.discarded_bytes == 0
    assert result.commands == len(SAMPLE)
    assert store.get(b"stats") is not None


def test_something_valid_but_not_a_command_ends_the_trusted_part(tmp_path, clock):
    path = tmp_path / "x.aof"
    path.write_bytes(ENCODED[0] + b"+OK\r\n" + ENCODED[1])

    store, result = recover(path, clock)

    assert result.commands == 1
    assert "not a command" in result.reason
    assert path.read_bytes() == ENCODED[0]


def test_an_empty_array_is_not_a_command_either(tmp_path, clock):
    path = tmp_path / "x.aof"
    path.write_bytes(ENCODED[0] + b"*0\r\n")

    _, result = recover(path, clock)

    assert result.commands == 1
    assert path.read_bytes() == ENCODED[0]


def test_a_file_of_pure_garbage_recovers_to_an_empty_dataset(tmp_path, clock):
    path = tmp_path / "x.aof"
    garbage = os.urandom(5000).replace(b"*", b"-")  # random bytes that cannot start an array
    path.write_bytes(garbage)

    store, result = recover(path, clock)

    assert len(store) == 0
    assert result.commands == 0
    assert path.read_bytes() == b""
    assert discarded(path) == garbage


def test_an_empty_file_is_fine(tmp_path, clock):
    path = tmp_path / "x.aof"
    path.write_bytes(b"")

    store, result = recover(path, clock)

    assert (result.commands, result.discarded_bytes) == (0, 0)


def test_what_is_thrown_away_in_several_recoveries_is_all_kept(tmp_path, clock):
    path = tmp_path / "x.aof"
    path.write_bytes(ENCODED[0] + b"junk-one")
    recover(path, clock)
    with open(path, "ab") as file:
        file.write(b"junk-two")
    recover(path, clock)

    assert discarded(path) == b"junk-one" + b"junk-two"


def test_the_commands_before_a_cut_are_replayed_even_if_a_later_one_fails(tmp_path, clock):
    path = tmp_path / "x.aof"
    path.write_bytes(
        encode_command("SET", "word", "hello")
        + encode_command("INCR", "word")  # fails when replayed: "hello" is not a number
        + encode_command("SET", "after", "1")
        + encode_command("SET", "cut", "off")[:-5]
    )

    store, result = recover(path, clock)

    assert (result.commands, result.errors) == (3, 1)
    assert store.get(b"after") == b"1"
    assert store.get(b"cut") is None


# ---- the server starting on a damaged file --------------------------------------------------


def test_the_server_starts_on_a_cut_log_and_keeps_going(aof_path, make_server, make_client):
    aof_path.write_bytes(LOG[: ENDS[2] + 9])  # three whole commands and a bit of the fourth
    server = make_server(aof_path=aof_path)
    c = make_client(server.address)

    assert server.redis.aof_load.commands == 3
    assert server.redis.aof_load.discarded_bytes == 9
    assert c.call("GET", "name") == b"vamsi"
    assert c.call("LRANGE", "queue", 0, -1) == [b"three", b"two", b"one"]
    assert c.call("SET", "new", "write") is not None
    server.stop()

    again = make_server(aof_path=aof_path)  # and that new write is good on the next start
    assert again.redis.aof_load.discarded_bytes == 0
    assert make_client(again.address).call("GET", "new") == b"write"


def test_the_server_starts_on_a_file_of_zeros(aof_path, make_server, make_client):
    aof_path.write_bytes(b"\x00" * 1000)

    server = make_server(aof_path=aof_path)

    assert make_client(server.address).call("PING") is not None
    assert server.redis.aof_load.discarded_bytes == 1000
    assert len(server.store) == 0


# ---- killing a real server process ----------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_process(path, port, policy):
    process = subprocess.Popen(
        [
            sys.executable, "-m", "miniredis.server",
            "--port", str(port), "--aof-file", str(path), "--appendfsync", policy,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return process
        except OSError:
            if process.poll() is not None:
                raise RuntimeError("the server process exited while starting") from None
            time.sleep(0.05)
    process.kill()
    raise RuntimeError("the server process did not start")


@pytest.mark.parametrize("policy", ["always", "everysec", "no"])
def test_everything_a_killed_server_acknowledged_is_still_there_after_it_restarts(
    policy, tmp_path, make_client, clock
):
    """Kill -9 in the middle of a stream of writes. Every write the server said OK to must be in
    the log, whatever the fsync policy: the log is written before the reply is sent."""
    path = tmp_path / "killed.aof"
    port = free_port()
    process = start_process(path, port, policy)
    try:
        client = make_client(("127.0.0.1", port))
        total, acknowledged = 20_000, 1_500
        client.send_raw(
            b"".join(encode_command("SET", f"key{i}", f"value-{i}") for i in range(total))
        )
        for _ in range(acknowledged):
            client.read(timeout=20)  # the server said OK to at least these
        time.sleep(random.uniform(0, 0.05))  # let it get further, by a different amount each time
        process.kill()  # no chance to flush, close or clean up anything
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()

    store, result = recover(path, clock)

    assert result.commands >= acknowledged
    for i in range(acknowledged):
        assert store.get(f"key{i}".encode()) == f"value-{i}".encode(), f"key{i} was lost"
    # whatever was in flight is either completely there or completely gone, never half of it
    for i in range(result.commands):
        assert store.get(f"key{i}".encode()) == f"value-{i}".encode()


def test_a_killed_server_restarts_and_carries_on(tmp_path, make_client):
    path = tmp_path / "killed.aof"
    port = free_port()
    process = start_process(path, port, "everysec")
    try:
        client = make_client(("127.0.0.1", port))
        for i in range(200):
            client.call("INCR", "visits")
            client.call("LPUSH", "events", f"e{i}")
        client.call("SET", "temp", "v", "EX", 1000)
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()

    again = start_process(path, port, "everysec")
    try:
        c = make_client(("127.0.0.1", port))
        assert c.call("GET", "visits") == b"200"
        assert c.call("LLEN", "events") == 200
        assert c.call("RPOP", "events") == b"e0"
        assert 990 <= c.call("TTL", "temp") <= 1000
        assert c.call("INCR", "visits") == 201
    finally:
        again.kill()
        again.wait(timeout=10)

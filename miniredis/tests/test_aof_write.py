import os
import time

import pytest

from miniredis.aof import AppendOnlyFile
from miniredis.commands import ClientState, Context, execute
from miniredis.resp import encode_command
from miniredis.store import Store


@pytest.fixture
def aof(aof_path):
    log = AppendOnlyFile(aof_path)
    log.open()
    yield log
    log.close()


class CountFsyncs:
    """Replaces os.fsync to count calls, and still really syncs."""

    def __init__(self, monkeypatch):
        self.calls = 0
        real = os.fsync

        def counting(fd):
            self.calls += 1
            real(fd)

        monkeypatch.setattr(os, "fsync", counting)


# ---- the file writer ------------------------------------------------------------------------


def test_open_creates_the_file_and_its_folder(tmp_path):
    path = tmp_path / "deeper" / "folders" / "log.aof"
    log = AppendOnlyFile(path)

    log.open()

    assert path.exists()
    assert path.read_bytes() == b""
    log.close()


def test_appended_commands_reach_the_file_only_at_commit(aof, aof_path):
    aof.append([b"SET", b"a", b"1"])
    assert aof_path.read_bytes() == b""
    assert aof.pending

    aof.commit()

    assert aof_path.read_bytes() == b"*3\r\n$3\r\nSET\r\n$1\r\na\r\n$1\r\n1\r\n"
    assert not aof.pending


def test_commits_append_in_order(aof, aof_path):
    aof.append([b"SET", b"a", b"1"])
    aof.append([b"INCR", b"a"])
    aof.commit()
    aof.append([b"DEL", b"a"])
    aof.commit()

    assert aof_path.read_bytes() == (
        encode_command("SET", "a", "1") + encode_command("INCR", "a") + encode_command("DEL", "a")
    )


def test_commit_with_nothing_to_write_does_nothing(aof, aof_path):
    aof.commit()

    assert aof_path.read_bytes() == b""
    assert aof.size == 0


def test_size_follows_the_file(aof, aof_path):
    aof.append([b"SET", b"key", b"value"])
    aof.commit()

    assert aof.size == len(aof_path.read_bytes()) > 0


def test_opening_an_existing_file_appends_to_it(aof_path):
    aof_path.write_bytes(encode_command("SET", "old", "1"))
    log = AppendOnlyFile(aof_path)
    log.open()
    assert log.size == len(encode_command("SET", "old", "1"))

    log.append([b"SET", b"new", b"2"])
    log.commit()
    log.close()

    assert aof_path.read_bytes() == encode_command("SET", "old", "1") + encode_command(
        "SET", "new", "2"
    )


def test_binary_safe_commands_are_logged_exactly(aof, aof_path):
    value = b"line1\r\nline2\x00\xff"
    aof.append([b"SET", b"k\r\n", value])
    aof.commit()

    assert aof_path.read_bytes() == encode_command(b"SET", b"k\r\n", value)


def test_close_writes_what_is_still_pending(aof_path):
    log = AppendOnlyFile(aof_path)
    log.open()
    log.append([b"SET", b"a", b"1"])

    log.close()

    assert aof_path.read_bytes() == encode_command("SET", "a", "1")
    log.close()  # closing twice is harmless


def test_an_unknown_fsync_policy_is_refused(aof_path):
    with pytest.raises(ValueError):
        AppendOnlyFile(aof_path, fsync="sometimes")


# ---- fsync policies -------------------------------------------------------------------------


def test_policy_always_syncs_on_every_commit(aof_path, monkeypatch):
    fsyncs = CountFsyncs(monkeypatch)
    log = AppendOnlyFile(aof_path, fsync="always")
    log.open()

    for _ in range(3):
        log.append([b"SET", b"a", b"1"])
        log.commit()

    assert fsyncs.calls == 3
    log.close()


def test_policy_everysec_leaves_syncing_to_the_background_task(aof_path, monkeypatch):
    fsyncs = CountFsyncs(monkeypatch)
    log = AppendOnlyFile(aof_path, fsync="everysec")
    log.open()

    log.append([b"SET", b"a", b"1"])
    log.commit()
    assert fsyncs.calls == 0

    log.sync()  # what the background task does once a second
    log.sync()  # nothing new was written, so there is nothing to sync
    assert fsyncs.calls == 1
    log.append([b"SET", b"b", b"2"])
    log.commit()
    log.sync()
    assert fsyncs.calls == 2
    log.close()


def test_policy_no_never_syncs_while_running(aof_path, monkeypatch):
    fsyncs = CountFsyncs(monkeypatch)
    log = AppendOnlyFile(aof_path, fsync="no")
    log.open()

    log.append([b"SET", b"a", b"1"])
    log.commit()

    assert fsyncs.calls == 0
    log.close()
    assert fsyncs.calls == 1  # but a clean shutdown always syncs once


# ---- trouble --------------------------------------------------------------------------------


def test_a_failed_write_keeps_the_data_and_the_next_commit_retries(aof, aof_path, monkeypatch):
    real_write = os.write
    failures = [True]

    def flaky_write(fd, data):
        if failures:
            failures.pop()
            raise OSError("No space left on device")
        return real_write(fd, data)

    aof.append([b"SET", b"a", b"1"])
    monkeypatch.setattr(os, "write", flaky_write)

    with pytest.raises(OSError, match="No space"):
        aof.commit()
    assert aof.error == "No space left on device"
    assert aof.pending
    assert aof_path.read_bytes() == b""

    aof.append([b"SET", b"b", b"2"])
    aof.commit()  # the disk is fine again: both commands are written, in order

    assert aof.error is None
    assert aof_path.read_bytes() == encode_command("SET", "a", "1") + encode_command(
        "SET", "b", "2"
    )


def test_a_write_that_only_takes_part_of_the_data_is_completed(aof, aof_path, monkeypatch):
    real_write = os.write

    def three_bytes_at_a_time(fd, data):
        return real_write(fd, data[:3])

    monkeypatch.setattr(os, "write", three_bytes_at_a_time)
    aof.append([b"SET", b"hello", b"world"])

    aof.commit()

    assert aof_path.read_bytes() == encode_command("SET", "hello", "world")


def test_a_failure_halfway_through_a_write_does_not_duplicate_what_was_written(
    aof, aof_path, monkeypatch
):
    real_write = os.write
    calls = []

    def writes_five_bytes_then_fails(fd, data):
        calls.append(1)
        if len(calls) == 1:
            return real_write(fd, data[:5])
        if len(calls) == 2:
            raise OSError("disk full")
        return real_write(fd, data)

    aof.append([b"SET", b"hello", b"world"])
    monkeypatch.setattr(os, "write", writes_five_bytes_then_fails)

    with pytest.raises(OSError):
        aof.commit()
    aof.commit()

    assert aof_path.read_bytes() == encode_command("SET", "hello", "world")


# ---- what each command logs -----------------------------------------------------------------


class RecordingLog:
    def __init__(self):
        self.commands: list[list[bytes]] = []

    def append(self, words):
        self.commands.append(words)


@pytest.fixture
def logged(clock):
    log = RecordingLog()
    ctx = Context(store=Store(clock=clock), client=ClientState(id=1), aof=log)

    def run(*words):
        log.commands.clear()
        reply = execute(ctx, [w if isinstance(w, bytes) else str(w).encode() for w in words])
        return reply, [[w.decode() for w in c] for c in log.commands]

    run.ctx = ctx
    return run


def test_set_is_logged_as_given(logged):
    assert logged("SET", "a", "1")[1] == [["SET", "a", "1"]]


def test_set_with_a_relative_expiry_is_logged_with_an_absolute_time(logged, clock):
    _, log = logged("SET", "a", "1", "EX", 10)
    assert log == [["SET", "a", "1", "PXAT", str(clock.now + 10_000)]]
    _, log = logged("SET", "b", "1", "PX", 250)
    assert log == [["SET", "b", "1", "PXAT", str(clock.now + 250)]]
    _, log = logged("SET", "c", "1", "EXAT", (clock.now + 99_000) // 1000)
    assert log == [["SET", "c", "1", "PXAT", str((clock.now + 99_000) // 1000 * 1000)]]


def test_set_nx_and_xx_are_logged_without_them_once_they_succeeded(logged):
    assert logged("SET", "a", "1", "NX")[1] == [["SET", "a", "1"]]
    assert logged("SET", "a", "2", "XX")[1] == [["SET", "a", "2"]]


def test_a_set_that_did_nothing_logs_nothing(logged):
    logged("SET", "a", "1")

    assert logged("SET", "a", "2", "NX") == (None, [])
    assert logged("SET", "missing", "2", "XX") == (None, [])


def test_set_keepttl_is_logged_as_such(logged):
    assert logged("SET", "a", "1", "KEEPTTL")[1] == [["SET", "a", "1", "KEEPTTL"]]


def test_set_with_a_time_in_the_past_is_logged_as_a_delete(logged, clock):
    logged("SET", "a", "1")

    assert logged("SET", "a", "2", "PXAT", clock.now - 5)[1] == [["DEL", "a"]]
    assert logged("SET", "never-existed", "2", "PXAT", clock.now - 5)[1] == []


def test_del_logs_only_the_keys_that_existed(logged):
    logged("SET", "a", "1")
    logged("SET", "b", "1")

    assert logged("DEL", "a", "nothing", "b", "a")[1] == [["DEL", "a", "b"]]
    assert logged("DEL", "nothing")[1] == []


def test_incr_and_incrby_log_the_result_and_keep_the_expiry(logged):
    assert logged("INCR", "n")[1] == [["SET", "n", "1", "KEEPTTL"]]
    assert logged("INCRBY", "n", 10)[1] == [["SET", "n", "11", "KEEPTTL"]]


def test_a_failed_incr_logs_nothing(logged):
    logged("SET", "word", "hello")

    reply, log = logged("INCR", "word")

    assert hasattr(reply, "message")
    assert log == []


def test_expire_is_logged_with_an_absolute_time(logged, clock):
    logged("SET", "a", "1")

    assert logged("EXPIRE", "a", 100)[1] == [["PEXPIREAT", "a", str(clock.now + 100_000)]]
    assert logged("PEXPIRE", "a", 250)[1] == [["PEXPIREAT", "a", str(clock.now + 250)]]
    assert logged("PEXPIREAT", "a", clock.now + 5)[1] == [["PEXPIREAT", "a", str(clock.now + 5)]]


def test_expire_that_deletes_is_logged_as_a_delete(logged):
    logged("SET", "a", "1")

    assert logged("EXPIRE", "a", 0)[1] == [["DEL", "a"]]


def test_an_expire_that_changed_nothing_logs_nothing(logged):
    assert logged("EXPIRE", "missing", 10)[1] == []
    logged("SET", "a", "1")
    assert logged("EXPIRE", "a", 10, "XX")[1] == []


def test_persist_is_logged_only_if_it_removed_an_expiry(logged):
    logged("SET", "a", "1", "EX", 100)

    assert logged("PERSIST", "a")[1] == [["PERSIST", "a"]]
    assert logged("PERSIST", "a")[1] == []


def test_lpush_and_hset_are_logged_as_given(logged):
    assert logged("LPUSH", "l", "a", "b")[1] == [["LPUSH", "l", "a", "b"]]
    assert logged("HSET", "h", "f", "v", "g", "w")[1] == [["HSET", "h", "f", "v", "g", "w"]]


def test_rpop_logs_exactly_what_was_taken(logged):
    logged("LPUSH", "l", "a", "b", "c")

    assert logged("RPOP", "l")[1] == [["RPOP", "l"]]
    assert logged("RPOP", "l", 100)[1] == [["RPOP", "l", "2"]]  # only 2 were left
    assert logged("RPOP", "l")[1] == []
    assert logged("RPOP", "l", 5)[1] == []
    assert logged("RPOP", "missing", 5)[1] == []


def test_reads_and_errors_log_nothing(logged):
    logged("SET", "s", "v")
    logged("LPUSH", "l", "x")
    for words in (
        ["GET", "s"], ["EXISTS", "s"], ["TTL", "s"], ["PTTL", "s"], ["TYPE", "s"], ["LLEN", "l"],
        ["LRANGE", "l", 0, -1], ["HGET", "h", "f"], ["HGETALL", "h"], ["PING"], ["ECHO", "x"],
        ["GET", "l"], ["LPUSH", "s", "x"], ["HSET", "s", "f", "v"], ["FOO"], ["GET"],
        ["CLIENT", "SETNAME", "x"], ["HELLO", "2"], ["COMMAND", "COUNT"],
    ):  # fmt: skip
        assert logged(*words)[1] == [], words


# ---- through the real server ----------------------------------------------------------------


def test_the_log_is_written_before_the_reply_is_sent(aof_client, aof_path):
    assert aof_client.call("SET", "a", "1") is not None

    # no shutdown, no waiting: the moment the client has its reply, the change is in the file
    assert aof_path.read_bytes() == encode_command("SET", "a", "1")


def test_a_pipelined_batch_is_logged_in_order(aof_client, aof_path):
    aof_client.send_raw(
        encode_command("SET", "a", "1") + encode_command("INCR", "a") + encode_command("GET", "a")
    )
    [aof_client.read() for _ in range(3)]

    assert aof_path.read_bytes() == (
        encode_command("SET", "a", "1") + encode_command("SET", "a", "2", "KEEPTTL")
    )  # the GET is not logged


def test_reads_and_failures_do_not_touch_the_file(aof_client, aof_path):
    aof_client.call("GET", "x")
    aof_client.call("FOO")
    aof_client.call("INCR")

    assert aof_path.read_bytes() == b""


def test_when_the_file_cannot_be_written_the_client_gets_an_error_not_a_reply(
    aof_server, aof_client, monkeypatch
):
    def broken_write(fd, data):
        raise OSError("No space left on device")

    monkeypatch.setattr(os, "write", broken_write)

    reply = aof_client.call("SET", "a", "1")

    assert reply.message.startswith("MISCONF Errors writing to the AOF file")
    assert "No space left on device" in reply.message


def test_everysec_syncs_in_the_background(aof_client, monkeypatch):
    fsyncs = CountFsyncs(monkeypatch)
    aof_client.call("SET", "a", "1")
    assert fsyncs.calls == 0  # the reply did not wait for a sync

    deadline = time.monotonic() + 3
    while fsyncs.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert fsyncs.calls >= 1


def test_policy_always_syncs_before_replying(aof_path, make_server, make_client, monkeypatch):
    fsyncs = CountFsyncs(monkeypatch)
    client = make_client(make_server(aof_path=aof_path, appendfsync="always").address)
    before = fsyncs.calls

    client.call("SET", "a", "1")

    assert fsyncs.calls == before + 1


def test_shutting_down_closes_the_file(aof_server, aof_client, aof_path):
    aof_client.call("SET", "a", "1")

    aof_server.stop()

    assert aof_server.redis.aof._fd is None
    assert aof_path.read_bytes() == encode_command("SET", "a", "1")

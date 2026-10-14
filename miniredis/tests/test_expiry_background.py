import time

import pytest

from miniredis.resp import OK, encode_command
from miniredis.store import Store


@pytest.fixture
def store(clock):
    return Store(clock=clock)


# ---- the cleaner's logic on the store itself ------------------------------------------------


def test_purge_removes_due_keys_without_anyone_reading_them(store, clock):
    store.set(b"a", b"1", expire_at=clock.now + 1_000)
    store.set(b"b", b"2", expire_at=clock.now + 5_000)
    store.set(b"c", b"3")
    clock.advance(2_000)

    assert len(store) == 3  # a is dead but still in memory: nobody has touched it
    assert store.purge_expired() == 1

    assert len(store) == 2
    assert store.expired_keys == 1


def test_purge_does_nothing_when_nothing_is_due(store, clock):
    store.set(b"a", b"1", expire_at=clock.now + 1_000)

    assert store.purge_expired() == 0
    assert len(store) == 1


def test_purge_removes_keys_in_order_as_time_passes(store, clock):
    for i in range(5):
        store.set(f"k{i}".encode(), b"v", expire_at=clock.now + (i + 1) * 1000)

    remaining = []
    for _ in range(5):
        clock.advance(1_000)
        store.purge_expired()
        remaining.append(len(store))

    assert remaining == [4, 3, 2, 1, 0]


def test_purge_looks_at_no_more_than_the_limit_each_time(store, clock):
    for i in range(10):
        store.set(f"k{i}".encode(), b"v", expire_at=clock.now + 10)
    clock.advance(100)

    assert store.purge_expired(limit=4) == 4
    assert store.has_due_expiry()
    assert store.purge_expired(limit=4) == 4
    assert store.purge_expired(limit=4) == 2
    assert not store.has_due_expiry()
    assert len(store) == 0


def test_a_key_given_a_later_time_is_not_killed_at_its_old_time(store, clock):
    store.set(b"k", b"v", expire_at=clock.now + 1_000)
    store.set_expiry(b"k", clock.now + 10_000)  # the first heap entry is now stale
    clock.advance(2_000)

    assert store.purge_expired() == 0
    assert store.get(b"k") == b"v"
    clock.advance(8_000)
    assert store.purge_expired() == 1


def test_a_persisted_key_is_not_killed(store, clock):
    store.set(b"k", b"v", expire_at=clock.now + 1_000)
    store.persist(b"k")
    clock.advance(5_000)

    assert store.purge_expired() == 0
    assert store.get(b"k") == b"v"


def test_a_deleted_then_recreated_key_is_not_killed_by_the_old_entry(store, clock):
    store.set(b"k", b"old", expire_at=clock.now + 1_000)
    store.delete(b"k")
    store.set(b"k", b"new")  # no expiry this time
    clock.advance(5_000)

    assert store.purge_expired() == 0
    assert store.get(b"k") == b"new"


def test_a_key_removed_lazily_leaves_only_a_harmless_stale_entry(store, clock):
    store.set(b"k", b"v", expire_at=clock.now + 1_000)
    clock.advance(2_000)
    assert store.get(b"k") is None  # removed lazily, and counted once

    assert store.purge_expired() == 0
    assert store.expired_keys == 1


def test_the_same_expiry_time_after_delete_and_reset_still_works(store, clock):
    at = clock.now + 1_000
    store.set(b"k", b"v", expire_at=at)
    store.delete(b"k")
    store.set(b"k", b"v2", expire_at=at)  # two heap entries with the same time and key
    clock.advance(2_000)

    assert store.purge_expired() == 1
    assert len(store) == 0
    assert store.purge_expired() == 0


def test_changing_one_keys_expiry_over_and_over_does_not_grow_memory_without_bound(store, clock):
    store.set(b"k", b"v", expire_at=clock.now + 1_000_000)

    for i in range(10_000):
        store.set_expiry(b"k", clock.now + 1_000_000 + i)

    assert len(store._heap) < 3_000  # stale entries were cleaned up along the way
    clock.advance(2_000_000)
    assert store.purge_expired() == 1


def test_purge_with_an_empty_store(store):
    assert store.purge_expired() == 0
    assert not store.has_due_expiry()


# ---- the cleaner running inside the real server ---------------------------------------------


def wait_until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_the_server_removes_expired_keys_that_nobody_reads(server, client):
    client.call("SET", "ghost", "v", "PX", 100)
    assert len(server.store) == 1

    # we never GET it: only the background cleaner can remove it from memory
    assert wait_until(lambda: len(server.store) == 0)
    assert server.store.expired_keys == 1


def test_keys_without_an_expiry_are_left_alone(server, client):
    client.call("SET", "forever", "v")
    client.call("SET", "brief", "v", "PX", 100)

    assert wait_until(lambda: len(server.store) == 1)
    assert client.call("GET", "forever") == b"v"


def test_thousands_of_keys_expiring_together_do_not_freeze_the_server(server, connect):
    loader, probe = connect(), connect()
    count = 20_000
    # all keys die at the very same moment; the margin is generous so a busy machine can still
    # finish loading them all before the first one dies
    dies_at = time.time_ns() // 1_000_000 + 5_000
    loader.send_raw(
        b"".join(encode_command("SET", f"k{i}", "v", "PXAT", dies_at) for i in range(count))
    )
    assert [loader.read(timeout=20) for _ in range(count)] == [OK] * count
    assert len(server.store) == count

    slowest = 0.0
    deadline = time.monotonic() + 10
    while len(server.store) > 0 and time.monotonic() < deadline:
        start = time.perf_counter()
        probe.call("PING")
        slowest = max(slowest, time.perf_counter() - start)
        time.sleep(0.005)

    assert len(server.store) == 0
    assert slowest < 1.0  # clients were served while the cleaner worked in small batches


def test_a_key_set_again_before_its_time_survives_the_cleaner(server, client):
    client.call("SET", "k", "first", "PX", 100)
    client.call("SET", "k", "second", "PX", 5000)

    time.sleep(0.4)

    assert client.call("GET", "k") == b"second"


def test_the_server_can_be_started_and_closed_cleanly():
    import asyncio

    from miniredis.server import start_server

    async def scenario():
        server = await start_server("127.0.0.1", 0)
        host, port = server.address
        assert host == "127.0.0.1"
        assert port > 0
        await server.close()

    asyncio.run(scenario())

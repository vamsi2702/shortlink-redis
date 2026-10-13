import pytest

from miniredis.commands import ClientState, Context, execute
from miniredis.resp import OK, SimpleString
from miniredis.store import Store

WRONGTYPE = "WRONGTYPE Operation against a key holding the wrong kind of value"


@pytest.fixture
def ctx(clock):
    return Context(store=Store(clock=clock), client=ClientState(id=1))


def run(ctx, *words):
    return execute(ctx, [w if isinstance(w, bytes) else str(w).encode() for w in words])


def message(reply) -> str:
    assert hasattr(reply, "message"), reply
    return reply.message


def test_hset_then_hget(ctx):
    assert run(ctx, "HSET", "h", "views", 42) == 1
    assert run(ctx, "HGET", "h", "views") == b"42"


def test_hget_of_a_missing_field_or_key_is_null(ctx):
    run(ctx, "HSET", "h", "f", "v")

    assert run(ctx, "HGET", "h", "nofield") is None
    assert run(ctx, "HGET", "nokey", "f") is None


def test_hset_counts_only_new_fields(ctx):
    assert run(ctx, "HSET", "h", "f", "v") == 1
    assert run(ctx, "HSET", "h", "f", "v2") == 0  # an update, not a new field
    assert run(ctx, "HGET", "h", "f") == b"v2"
    assert run(ctx, "HSET", "h", "g", 1, "h", 2) == 2
    assert run(ctx, "HSET", "h", "g", 9, "i", 3) == 1


def test_the_same_field_twice_in_one_command_counts_once_and_the_last_value_wins(ctx):
    assert run(ctx, "HSET", "h", "dup", 1, "dup", 2) == 1
    assert run(ctx, "HGET", "h", "dup") == b"2"


def test_hgetall_returns_field_value_pairs_in_the_order_they_were_added(ctx):
    for field in ("zebra", "apple", "mango", "banana"):
        run(ctx, "HSET", "h", field, field.upper())

    assert run(ctx, "HGETALL", "h") == [
        b"zebra", b"ZEBRA", b"apple", b"APPLE", b"mango", b"MANGO", b"banana", b"BANANA",
    ]  # fmt: skip


def test_updating_a_field_keeps_its_place(ctx):
    run(ctx, "HSET", "h", "a", 1, "b", 2, "c", 3)

    run(ctx, "HSET", "h", "a", "changed")

    assert run(ctx, "HGETALL", "h") == [b"a", b"changed", b"b", b"2", b"c", b"3"]


def test_hgetall_of_a_missing_key_is_empty(ctx):
    assert run(ctx, "HGETALL", "nothing") == []


def test_empty_field_and_value_are_allowed(ctx):
    assert run(ctx, "HSET", "h", "", "") == 1
    assert run(ctx, "HGET", "h", "") == b""


def test_fields_and_values_are_binary_safe(ctx):
    field, value = b"f\r\nx", b"v\x00\xffy"
    run(ctx, b"HSET", b"h", field, value)

    assert run(ctx, b"HGET", b"h", field) == value
    assert run(ctx, b"HGETALL", b"h") == [field, value]


def test_type_is_hash(ctx):
    run(ctx, "HSET", "h", "f", "v")

    assert run(ctx, "TYPE", "h") == SimpleString("hash")


@pytest.mark.parametrize(
    ("words", "name"),
    [
        (["HSET"], "hset"),
        (["HSET", "h"], "hset"),
        (["HSET", "h", "f"], "hset"),
        (["HSET", "h", "f", "v", "g"], "hset"),
        (["HGET"], "hget"),
        (["HGET", "h"], "hget"),
        (["HGET", "h", "f", "x"], "hget"),
        (["HGETALL"], "hgetall"),
        (["HGETALL", "h", "x"], "hgetall"),
    ],
)
def test_wrong_number_of_arguments(ctx, words, name):
    assert message(run(ctx, *words)) == f"ERR wrong number of arguments for '{name}' command"
    assert run(ctx, "EXISTS", "h") == 0  # and nothing was created


@pytest.mark.parametrize(
    "words", [["HSET", "k", "f", "v"], ["HGET", "k", "f"], ["HGETALL", "k"]]
)
@pytest.mark.parametrize("setup", [["SET", "k", "v"], ["LPUSH", "k", "x"]])
def test_hash_commands_refuse_other_types(ctx, setup, words):
    run(ctx, *setup)

    assert message(run(ctx, *words)) == WRONGTYPE


@pytest.mark.parametrize(
    "words",
    [
        ["GET", "k"],
        ["INCR", "k"],
        ["INCRBY", "k", "2"],
        ["LLEN", "k"],
        ["LPUSH", "k", "x"],
        ["RPOP", "k"],
        ["RPOP", "k", "2"],
        ["LRANGE", "k", "0", "-1"],
    ],
)
def test_string_and_list_commands_refuse_a_hash(ctx, words):
    run(ctx, "HSET", "k", "f", "v")

    assert message(run(ctx, *words)) == WRONGTYPE
    assert run(ctx, "HGET", "k", "f") == b"v"  # untouched


def test_set_replaces_a_hash_with_a_string(ctx):
    run(ctx, "HSET", "k", "f", "v")

    assert run(ctx, "SET", "k", "string now") == OK
    assert run(ctx, "TYPE", "k") == SimpleString("string")


def test_a_hash_can_expire_and_keeps_its_expiry_when_changed(ctx, clock):
    run(ctx, "HSET", "h", "f", "v")
    run(ctx, "EXPIRE", "h", 100)
    clock.advance(40_000)

    run(ctx, "HSET", "h", "g", "w")
    assert run(ctx, "TTL", "h") == 60
    clock.advance(60_000)

    assert run(ctx, "HGETALL", "h") == []
    assert run(ctx, "EXISTS", "h") == 0


def test_del_removes_a_hash(ctx):
    run(ctx, "HSET", "h", "f", "v")

    assert run(ctx, "DEL", "h") == 1
    assert run(ctx, "HGETALL", "h") == []


def test_a_dead_hash_is_replaced_by_a_new_one(ctx, clock):
    run(ctx, "HSET", "h", "old", 1)
    run(ctx, "EXPIRE", "h", 1)
    clock.advance(1_000)

    assert run(ctx, "HSET", "h", "new", 1) == 1
    assert run(ctx, "HGETALL", "h") == [b"new", b"1"]
    assert run(ctx, "TTL", "h") == -1


def test_a_hash_with_many_fields(ctx):
    for i in range(2000):
        run(ctx, "HSET", "big", f"field{i}", i)

    assert run(ctx, "HGET", "big", "field1234") == b"1234"
    assert len(run(ctx, "HGETALL", "big")) == 4000


def test_hashes_through_the_real_server(client):
    assert client.call("HSET", "stats:abc12", "views", 42, "last", "10:05") == 2
    assert client.call("HGET", "stats:abc12", "views") == b"42"
    assert client.call("HGETALL", "stats:abc12") == [b"views", b"42", b"last", b"10:05"]

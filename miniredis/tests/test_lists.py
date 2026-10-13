import threading

import pytest

from miniredis.commands import ClientState, Context, execute
from miniredis.resp import NULL_ARRAY, OK, SimpleString, encode, parse
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


# ---- LPUSH, RPOP, LLEN ----------------------------------------------------------------------


def test_lpush_returns_the_new_length(ctx):
    assert run(ctx, "LPUSH", "l", "a") == 1
    assert run(ctx, "LPUSH", "l", "b", "c") == 3
    assert run(ctx, "LLEN", "l") == 3


def test_lpush_puts_each_value_in_front_of_the_one_before(ctx):
    run(ctx, "LPUSH", "l", "a")
    run(ctx, "LPUSH", "l", "b", "c")

    assert run(ctx, "LRANGE", "l", 0, -1) == [b"c", b"b", b"a"]


def test_rpop_takes_from_the_other_end_so_it_is_a_queue(ctx):
    for item in ("first", "second", "third"):
        run(ctx, "LPUSH", "queue", item)

    assert [run(ctx, "RPOP", "queue") for _ in range(3)] == [b"first", b"second", b"third"]


def test_rpop_on_a_missing_or_emptied_list_is_null(ctx):
    assert run(ctx, "RPOP", "nothing") is None
    run(ctx, "LPUSH", "l", "a")
    run(ctx, "RPOP", "l")

    assert run(ctx, "RPOP", "l") is None


def test_llen_of_a_missing_list_is_zero(ctx):
    assert run(ctx, "LLEN", "nothing") == 0


def test_an_emptied_list_disappears(ctx):
    run(ctx, "LPUSH", "l", "a")
    run(ctx, "RPOP", "l")

    assert run(ctx, "EXISTS", "l") == 0
    assert run(ctx, "TYPE", "l") == SimpleString("none")
    assert len(ctx.store) == 0


def test_the_empty_string_is_a_valid_item(ctx):
    run(ctx, "LPUSH", "l", "")

    assert run(ctx, "LRANGE", "l", 0, -1) == [b""]
    assert run(ctx, "RPOP", "l") == b""


def test_items_are_binary_safe(ctx):
    item = b"line1\r\nline2\x00\xff"
    run(ctx, b"LPUSH", b"l", item)

    assert run(ctx, b"RPOP", b"l") == item


# ---- RPOP with a count ----------------------------------------------------------------------


@pytest.fixture
def five(ctx):
    run(ctx, "LPUSH", "l", 1, 2, 3, 4, 5)  # the list is 5 4 3 2 1; RPOP gives 1 first


def test_rpop_with_a_count_returns_an_array_oldest_first(ctx, five):
    assert run(ctx, "RPOP", "l", 2) == [b"1", b"2"]
    assert run(ctx, "LLEN", "l") == 3


def test_rpop_with_a_count_bigger_than_the_list_takes_everything_and_deletes_the_key(ctx, five):
    assert run(ctx, "RPOP", "l", 100) == [b"1", b"2", b"3", b"4", b"5"]
    assert run(ctx, "EXISTS", "l") == 0


def test_rpop_with_count_zero_returns_an_empty_array_and_changes_nothing(ctx, five):
    assert run(ctx, "RPOP", "l", 0) == []
    assert run(ctx, "LLEN", "l") == 5


def test_rpop_with_a_count_on_a_missing_list_is_a_null_array(ctx):
    assert run(ctx, "RPOP", "nothing", 2) is NULL_ARRAY
    assert run(ctx, "RPOP", "nothing", 0) is NULL_ARRAY
    assert encode(NULL_ARRAY) == b"*-1\r\n"
    assert parse(b"*-1\r\n") is None


@pytest.mark.parametrize("count", ["-1", "abc", "1.5", "", "+1"])
def test_rpop_refuses_a_bad_count(ctx, five, count):
    assert message(run(ctx, "RPOP", "l", count)) == "ERR value is out of range, must be positive"
    assert run(ctx, "LLEN", "l") == 5


def test_rpop_with_the_biggest_possible_count(ctx, five):
    assert len(run(ctx, "RPOP", "l", 9223372036854775807)) == 5


# ---- LRANGE ---------------------------------------------------------------------------------


@pytest.fixture
def abcde(ctx):
    run(ctx, "LPUSH", "l", "e", "d", "c", "b", "a")  # the list reads a b c d e


@pytest.mark.parametrize(
    ("start", "stop", "expected"),
    [
        (0, -1, [b"a", b"b", b"c", b"d", b"e"]),
        (1, 2, [b"b", b"c"]),
        (-2, -1, [b"d", b"e"]),
        (-100, 100, [b"a", b"b", b"c", b"d", b"e"]),
        (3, 1, []),
        (10, 20, []),
        (0, 0, [b"a"]),
        (-1, -1, [b"e"]),
        (4, 100, [b"e"]),
        (5, 100, []),
        (0, -100, []),
        (9223372036854775807, 9223372036854775807, []),
        (-9223372036854775808, -1, [b"a", b"b", b"c", b"d", b"e"]),
    ],
)
def test_lrange(ctx, abcde, start, stop, expected):
    assert run(ctx, "LRANGE", "l", start, stop) == expected


def test_lrange_of_a_missing_list_is_empty(ctx):
    assert run(ctx, "LRANGE", "nothing", 0, -1) == []


@pytest.mark.parametrize(("start", "stop"), [("a", "1"), ("0", "1.5"), ("", "1")])
def test_lrange_refuses_non_integers(ctx, abcde, start, stop):
    assert message(run(ctx, "LRANGE", "l", start, stop)) == (
        "ERR value is not an integer or out of range"
    )


# ---- arity ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "name"),
    [
        (["LPUSH"], "lpush"),
        (["LPUSH", "l"], "lpush"),
        (["RPOP"], "rpop"),
        (["RPOP", "l", "1", "2"], "rpop"),
        (["LLEN"], "llen"),
        (["LLEN", "l", "x"], "llen"),
        (["LRANGE", "l", "0"], "lrange"),
        (["LRANGE", "l", "0", "1", "2"], "lrange"),
        (["TYPE"], "type"),
        (["TYPE", "a", "b"], "type"),
    ],
)
def test_wrong_number_of_arguments(ctx, words, name):
    assert message(run(ctx, *words)) == f"ERR wrong number of arguments for '{name}' command"


# ---- types ----------------------------------------------------------------------------------


def test_type(ctx):
    run(ctx, "SET", "s", "v")
    run(ctx, "LPUSH", "l", "x")

    assert run(ctx, "TYPE", "s") == SimpleString("string")
    assert run(ctx, "TYPE", "l") == SimpleString("list")
    assert run(ctx, "TYPE", "nothing") == SimpleString("none")


@pytest.mark.parametrize(
    "words",
    [
        ["LLEN", "s"],
        ["LPUSH", "s", "x"],
        ["RPOP", "s"],
        ["RPOP", "s", "2"],
        ["LRANGE", "s", "0", "-1"],
    ],
)
def test_list_commands_refuse_a_string(ctx, words):
    run(ctx, "SET", "s", "v")

    assert message(run(ctx, *words)) == WRONGTYPE
    assert run(ctx, "GET", "s") == b"v"  # untouched


@pytest.mark.parametrize("words", [["GET", "l"], ["INCR", "l"], ["INCRBY", "l", "5"]])
def test_string_commands_refuse_a_list(ctx, words):
    run(ctx, "LPUSH", "l", "x")

    assert message(run(ctx, *words)) == WRONGTYPE
    assert run(ctx, "LRANGE", "l", 0, -1) == [b"x"]  # untouched


def test_set_replaces_a_list_with_a_string(ctx):
    run(ctx, "LPUSH", "l", "x")

    assert run(ctx, "SET", "l", "now a string") == OK
    assert run(ctx, "TYPE", "l") == SimpleString("string")
    assert run(ctx, "GET", "l") == b"now a string"


def test_set_nx_does_not_replace_a_list(ctx):
    run(ctx, "LPUSH", "l", "x")

    assert run(ctx, "SET", "l", "v", "NX") is None
    assert run(ctx, "TYPE", "l") == SimpleString("list")


def test_del_and_exists_work_on_lists(ctx):
    run(ctx, "LPUSH", "l", "a", "b")

    assert run(ctx, "EXISTS", "l") == 1
    assert run(ctx, "DEL", "l") == 1
    assert run(ctx, "EXISTS", "l") == 0


# ---- expiry on lists ------------------------------------------------------------------------


def test_a_list_can_expire(ctx, clock):
    run(ctx, "LPUSH", "l", "a")
    assert run(ctx, "EXPIRE", "l", 10) == 1

    clock.advance(10_000)

    assert run(ctx, "LLEN", "l") == 0
    assert run(ctx, "EXISTS", "l") == 0


def test_pushing_keeps_the_lists_expiry(ctx, clock):
    run(ctx, "LPUSH", "l", "a")
    run(ctx, "EXPIRE", "l", 100)
    clock.advance(30_000)

    run(ctx, "LPUSH", "l", "b")

    assert run(ctx, "TTL", "l") == 70


def test_emptying_a_list_also_removes_its_expiry(ctx):
    run(ctx, "LPUSH", "l", "a")
    run(ctx, "EXPIRE", "l", 100)
    run(ctx, "RPOP", "l")

    assert run(ctx, "TTL", "l") == -2
    run(ctx, "LPUSH", "l", "fresh")
    assert run(ctx, "TTL", "l") == -1  # the new list has no expiry


def test_a_dead_list_is_replaced_by_a_new_one_on_push(ctx, clock):
    run(ctx, "LPUSH", "l", "old")
    run(ctx, "EXPIRE", "l", 1)
    clock.advance(1_000)

    assert run(ctx, "LPUSH", "l", "new") == 1
    assert run(ctx, "LRANGE", "l", 0, -1) == [b"new"]


# ---- bigger lists ---------------------------------------------------------------------------


def test_a_big_queue_keeps_its_order(ctx):
    for i in range(5000):
        run(ctx, "LPUSH", "q", i)

    assert run(ctx, "LLEN", "q") == 5000
    batch = run(ctx, "RPOP", "q", 5000)
    assert batch == [str(i).encode() for i in range(5000)]
    assert run(ctx, "EXISTS", "q") == 0


# ---- through the real server: a producer/consumer queue -------------------------------------


def test_producers_and_consumers_share_a_queue_without_losing_or_duplicating_items(server, connect):
    producers = 5
    per_producer = 400
    consumed: list[bytes] = []
    consumed_lock = threading.Lock()
    done_producing = threading.Event()

    def produce(n):
        client = connect()
        for i in range(per_producer):
            client.call("LPUSH", "clicks", f"{n}:{i}")

    def consume():
        client = connect()
        while True:
            batch = client.call("RPOP", "clicks", 25)
            if batch:
                with consumed_lock:
                    consumed.extend(batch)
            elif done_producing.is_set():
                return

    consumers = [threading.Thread(target=consume) for _ in range(3)]
    makers = [threading.Thread(target=produce, args=(n,)) for n in range(producers)]
    for t in consumers + makers:
        t.start()
    for t in makers:
        t.join(timeout=30)
    done_producing.set()
    for t in consumers:
        t.join(timeout=30)

    expected = {f"{n}:{i}".encode() for n in range(producers) for i in range(per_producer)}
    assert len(consumed) == producers * per_producer  # nothing lost, nothing delivered twice
    assert set(consumed) == expected


def test_null_and_null_array_are_different_on_the_wire(client):
    # the parser reads both as None, so look at the raw bytes the server really sent
    client.sock.sendall(b"*2\r\n$4\r\nRPOP\r\n$7\r\nmissing\r\n")
    assert client.sock.recv(100) == b"$-1\r\n"
    client.sock.sendall(b"*3\r\n$4\r\nRPOP\r\n$7\r\nmissing\r\n$1\r\n2\r\n")
    assert client.sock.recv(100) == b"*-1\r\n"

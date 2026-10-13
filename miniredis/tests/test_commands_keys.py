import threading

import pytest

from miniredis.commands import INT64_MAX, INT64_MIN, ClientState, Context, execute, parse_int64
from miniredis.resp import OK, RespError
from miniredis.store import Store

NOT_AN_INTEGER = RespError("ERR value is not an integer or out of range")
OVERFLOW = RespError("ERR increment or decrement would overflow")


@pytest.fixture
def ctx():
    return Context(store=Store(), client=ClientState(id=1))


def run(ctx, *words):
    return execute(ctx, [w if isinstance(w, bytes) else str(w).encode() for w in words])


# ---- DEL and EXISTS -------------------------------------------------------------------------


def test_del_returns_how_many_keys_existed(ctx):
    run(ctx, "SET", "a", "1")
    run(ctx, "SET", "b", "2")

    assert run(ctx, "DEL", "a", "b", "nothing") == 2
    assert run(ctx, "GET", "a") is None
    assert run(ctx, "GET", "b") is None


def test_del_of_a_missing_key_is_zero(ctx):
    assert run(ctx, "DEL", "nothing") == 0


def test_del_counts_a_key_listed_twice_once(ctx):
    run(ctx, "SET", "k", "1")

    assert run(ctx, "DEL", "k", "k") == 1
    assert run(ctx, "DEL", "k") == 0


def test_exists(ctx):
    run(ctx, "SET", "a", "1")

    assert run(ctx, "EXISTS", "a") == 1
    assert run(ctx, "EXISTS", "nothing") == 0


def test_exists_counts_a_key_listed_twice_twice(ctx):
    run(ctx, "SET", "a", "1")

    assert run(ctx, "EXISTS", "a", "a", "nothing") == 2


def test_a_key_with_an_empty_value_still_exists(ctx):
    run(ctx, "SET", "empty", "")

    assert run(ctx, "EXISTS", "empty") == 1


@pytest.mark.parametrize("name", ["DEL", "EXISTS"])
def test_del_and_exists_need_at_least_one_key(ctx, name):
    reply = run(ctx, name)

    assert reply == RespError(f"ERR wrong number of arguments for '{name.lower()}' command")


# ---- INCR and INCRBY ------------------------------------------------------------------------


def test_incr_creates_a_missing_key_at_one(ctx):
    assert run(ctx, "INCR", "n") == 1
    assert run(ctx, "GET", "n") == b"1"


def test_incr_counts_up(ctx):
    run(ctx, "SET", "n", "41")

    assert run(ctx, "INCR", "n") == 42
    assert run(ctx, "INCR", "n") == 43
    assert run(ctx, "GET", "n") == b"43"  # stored as text, replies as an integer


def test_incrby(ctx):
    assert run(ctx, "INCRBY", "n", 10) == 10
    assert run(ctx, "INCRBY", "n", -3) == 7
    assert run(ctx, "INCRBY", "n", 0) == 7


def test_incr_works_on_a_negative_number(ctx):
    run(ctx, "SET", "n", "-5")

    assert run(ctx, "INCR", "n") == -4


@pytest.mark.parametrize("stored", ["05", " 5", "+5", "5 ", "", "-0", "abc", "1.5", "1e3", "0x10"])
def test_incr_refuses_text_that_redis_does_not_accept_as_an_integer(ctx, stored):
    run(ctx, "SET", "n", stored)

    assert run(ctx, "INCR", "n") == NOT_AN_INTEGER
    assert run(ctx, "GET", "n") == stored.encode()  # untouched


def test_incr_accepts_zero(ctx):
    run(ctx, "SET", "n", "0")

    assert run(ctx, "INCR", "n") == 1


def test_incr_at_the_top_of_the_range_overflows(ctx):
    run(ctx, "SET", "n", str(INT64_MAX))

    assert run(ctx, "INCR", "n") == OVERFLOW
    assert run(ctx, "GET", "n") == str(INT64_MAX).encode()


def test_incr_at_the_bottom_of_the_range_is_fine(ctx):
    run(ctx, "SET", "n", str(INT64_MIN))

    assert run(ctx, "INCR", "n") == INT64_MIN + 1


def test_incrby_overflow_in_both_directions(ctx):
    run(ctx, "SET", "n", str(INT64_MAX - 1))
    assert run(ctx, "INCRBY", "n", 1) == INT64_MAX
    assert run(ctx, "INCRBY", "n", 1) == OVERFLOW

    run(ctx, "SET", "m", str(INT64_MIN + 1))
    assert run(ctx, "INCRBY", "m", -1) == INT64_MIN
    assert run(ctx, "INCRBY", "m", -1) == OVERFLOW


@pytest.mark.parametrize("amount", ["1.5", "abc", "", "9223372036854775808", " 1", "+1", "007"])
def test_incrby_refuses_a_bad_amount(ctx, amount):
    assert run(ctx, "INCRBY", "n", amount) == NOT_AN_INTEGER
    assert run(ctx, "EXISTS", "n") == 0


def test_incrby_accepts_the_smallest_64_bit_amount(ctx):
    assert run(ctx, "INCRBY", "n", str(INT64_MIN)) == INT64_MIN


def test_incr_on_a_key_holding_text(ctx):
    run(ctx, "SET", "word", "hi")

    assert run(ctx, "INCR", "word") == NOT_AN_INTEGER


def test_incr_arity(ctx):
    def arity_error(name):
        return RespError(f"ERR wrong number of arguments for '{name}' command")

    assert run(ctx, "INCR") == arity_error("incr")
    assert run(ctx, "INCR", "a", "b") == arity_error("incr")
    assert run(ctx, "INCRBY", "a") == arity_error("incrby")


def test_set_after_incr_replaces_the_number(ctx):
    run(ctx, "INCR", "n")

    assert run(ctx, "SET", "n", "hello") == OK
    assert run(ctx, "INCR", "n") == NOT_AN_INTEGER


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"0", 0),
        (b"7", 7),
        (b"-7", -7),
        (b"9223372036854775807", INT64_MAX),
        (b"-9223372036854775808", INT64_MIN),
        (b"9223372036854775808", None),
        (b"-9223372036854775809", None),
        (b"99999999999999999999", None),
        (b"", None),
        (b"-", None),
        (b"00", None),
        (b"-0", None),
        (b"+1", None),
        (b"1_000", None),
        (b"\xd9\xa1", None),  # an Arabic-Indic digit one: must not count as a digit
    ],
)
def test_parse_int64(raw, expected):
    assert parse_int64(raw) == expected


# ---- the payoff of "a command never awaits": counting is exact ------------------------------


def test_many_clients_incrementing_one_counter_lose_no_updates(server, connect):
    clients = [connect() for _ in range(20)]

    def worker(client):
        for _ in range(100):
            client.call("INCR", "hits")

    threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert connect().call("GET", "hits") == b"2000"


def test_pipelined_increments_from_many_clients_are_all_counted(server, connect):
    from miniredis.resp import encode_command

    clients = [connect() for _ in range(10)]
    for c in clients:
        c.send_raw(encode_command("INCR", "n") * 200)  # 200 INCRs in one send, per client

    replies = [[c.read() for _ in range(200)] for c in clients]

    assert connect().call("GET", "n") == b"2000"
    assert sorted(r for per_client in replies for r in per_client) == list(range(1, 2001))
    for per_client in replies:
        assert per_client == sorted(per_client)  # each client saw its own replies in order

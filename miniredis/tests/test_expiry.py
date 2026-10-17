import pytest

from miniredis.commands import INT64_MAX, ClientState, Context, execute
from miniredis.resp import OK, RespError
from miniredis.store import Store

NOT_AN_INTEGER = RespError("ERR value is not an integer or out of range")
SYNTAX = RespError("ERR syntax error")


@pytest.fixture
def ctx(clock):
    return Context(store=Store(clock=clock), client=ClientState(id=1))


def run(ctx, *words):
    return execute(ctx, [w if isinstance(w, bytes) else str(w).encode() for w in words])


def invalid_expire(command):
    return RespError(f"ERR invalid expire time in '{command}' command")


# ---- SET with an expiry ---------------------------------------------------------------------


def test_set_ex_sets_a_time_to_live_in_seconds(ctx):
    assert run(ctx, "SET", "k", "v", "EX", 100) == OK

    assert run(ctx, "TTL", "k") == 100
    assert run(ctx, "PTTL", "k") == 100_000


def test_set_px_is_in_milliseconds(ctx):
    run(ctx, "SET", "k", "v", "PX", 2500)

    assert run(ctx, "PTTL", "k") == 2500
    assert run(ctx, "TTL", "k") == 3  # rounded to the nearest second


def test_set_exat_and_pxat_take_a_time_of_day(ctx, clock):
    run(ctx, "SET", "a", "v", "EXAT", (clock.now + 50_000) // 1000)
    run(ctx, "SET", "b", "v", "PXAT", clock.now + 7_000)

    assert run(ctx, "TTL", "a") == 50
    assert run(ctx, "PTTL", "b") == 7000


def test_a_key_disappears_when_its_time_comes(ctx, clock):
    run(ctx, "SET", "k", "v", "EX", 10)

    clock.advance(9_999)
    assert run(ctx, "GET", "k") == b"v"
    clock.advance(1)  # exactly at the moment it dies
    assert run(ctx, "GET", "k") is None
    assert run(ctx, "EXISTS", "k") == 0
    assert run(ctx, "TTL", "k") == -2


def test_expiry_is_lazy_the_key_is_removed_when_it_is_touched(ctx, clock):
    run(ctx, "SET", "k", "v", "EX", 1)
    clock.advance(5_000)

    assert len(ctx.store) == 1  # nobody has looked at it yet
    assert run(ctx, "GET", "k") is None
    assert len(ctx.store) == 0
    assert ctx.store.expired_keys == 1


def test_a_plain_set_removes_the_expiry(ctx, clock):
    run(ctx, "SET", "k", "v", "EX", 10)
    run(ctx, "SET", "k", "new")

    clock.advance(60_000)

    assert run(ctx, "GET", "k") == b"new"
    assert run(ctx, "TTL", "k") == -1


def test_keepttl_keeps_the_expiry_of_the_old_value(ctx, clock):
    run(ctx, "SET", "k", "v", "EX", 50)
    clock.advance(10_000)

    assert run(ctx, "SET", "k", "v2", "KEEPTTL") == OK

    assert run(ctx, "GET", "k") == b"v2"
    assert run(ctx, "TTL", "k") == 40


def test_keepttl_does_not_bring_back_the_expiry_of_a_dead_key(ctx, clock):
    run(ctx, "SET", "k", "v", "EX", 1)
    clock.advance(2_000)

    run(ctx, "SET", "k", "fresh", "KEEPTTL")

    assert run(ctx, "TTL", "k") == -1
    assert run(ctx, "GET", "k") == b"fresh"


def test_incr_keeps_the_expiry_but_a_set_does_not(ctx, clock):
    run(ctx, "SET", "n", "5", "EX", 100)
    clock.advance(30_000)

    assert run(ctx, "INCR", "n") == 6
    assert run(ctx, "INCRBY", "n", 4) == 10
    assert run(ctx, "TTL", "n") == 70


def test_incr_on_a_dead_key_starts_again_from_zero_with_no_expiry(ctx, clock):
    run(ctx, "SET", "n", "41", "EX", 1)
    clock.advance(2_000)

    assert run(ctx, "INCR", "n") == 1
    assert run(ctx, "TTL", "n") == -1


def test_del_removes_the_key_and_its_expiry(ctx):
    run(ctx, "SET", "k", "v", "EX", 100)

    assert run(ctx, "DEL", "k") == 1
    run(ctx, "SET", "k", "again")

    assert run(ctx, "TTL", "k") == -1


def test_del_and_exists_do_not_count_a_dead_key(ctx, clock):
    run(ctx, "SET", "k", "v", "EX", 1)
    clock.advance(1_000)

    assert run(ctx, "EXISTS", "k") == 0
    assert run(ctx, "DEL", "k") == 0


# ---- NX and XX ------------------------------------------------------------------------------


def test_nx_sets_only_a_missing_key(ctx):
    assert run(ctx, "SET", "k", "first", "NX") == OK
    assert run(ctx, "SET", "k", "second", "NX") is None
    assert run(ctx, "GET", "k") == b"first"


def test_xx_sets_only_an_existing_key(ctx):
    assert run(ctx, "SET", "k", "v", "XX") is None
    assert run(ctx, "EXISTS", "k") == 0
    run(ctx, "SET", "k", "v")
    assert run(ctx, "SET", "k", "new", "XX") == OK
    assert run(ctx, "GET", "k") == b"new"


def test_nx_works_on_a_key_that_has_expired(ctx, clock):
    run(ctx, "SET", "k", "old", "EX", 1)
    clock.advance(1_000)

    assert run(ctx, "SET", "k", "new", "NX") == OK
    assert run(ctx, "GET", "k") == b"new"


def test_a_failed_nx_does_not_touch_the_existing_expiry_or_value(ctx):
    run(ctx, "SET", "k", "v", "EX", 100)

    assert run(ctx, "SET", "k", "other", "NX", "EX", 5) is None
    assert run(ctx, "GET", "k") == b"v"
    assert run(ctx, "TTL", "k") == 100


def test_the_rate_limiter_recipe_set_nx_ex_then_incr(ctx, clock):
    """The API's rate limiter uses this: the first request starts a window, later ones count."""
    assert run(ctx, "SET", "rl:ip", "1", "EX", 60, "NX") == OK
    assert run(ctx, "SET", "rl:ip", "1", "EX", 60, "NX") is None
    clock.advance(20_000)
    assert run(ctx, "INCR", "rl:ip") == 2
    assert run(ctx, "INCR", "rl:ip") == 3
    assert run(ctx, "TTL", "rl:ip") == 40  # the window did not restart
    clock.advance(40_000)
    assert run(ctx, "GET", "rl:ip") is None  # the window is over
    assert run(ctx, "SET", "rl:ip", "1", "EX", 60, "NX") == OK


# ---- option parsing -------------------------------------------------------------------------


@pytest.mark.parametrize("options", [["nx", "ex", "10"], ["EX", "10", "NX"], ["ex", "10", "nx"]])
def test_options_in_any_order_and_case(ctx, options):
    assert run(ctx, "SET", "k", "v", *options) == OK
    assert run(ctx, "TTL", "k") == 10


def test_a_repeated_option_is_fine_and_the_last_value_wins(ctx):
    assert run(ctx, "SET", "k", "v", "EX", 10, "EX", 20) == OK
    assert run(ctx, "TTL", "k") == 20
    assert run(ctx, "SET", "j", "v", "NX", "NX") == OK


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["EX"], SYNTAX),
        (["PX"], SYNTAX),
        (["EX", "abc"], NOT_AN_INTEGER),
        (["EX", "1.5"], NOT_AN_INTEGER),
        (["EX", " 10"], NOT_AN_INTEGER),
        (["EX", "+10"], NOT_AN_INTEGER),
        (["EX", "0"], invalid_expire("set")),
        (["EX", "-5"], invalid_expire("set")),
        (["PX", "0"], invalid_expire("set")),
        (["EXAT", "0"], invalid_expire("set")),
        (["PXAT", "-1"], invalid_expire("set")),
        (["EX", str(INT64_MAX)], invalid_expire("set")),
        (["PX", str(INT64_MAX)], invalid_expire("set")),
        (["EX", "10", "PX", "10"], SYNTAX),
        (["EX", "10", "EXAT", "10"], SYNTAX),
        (["NX", "XX"], SYNTAX),
        (["XX", "NX"], SYNTAX),
        (["KEEPTTL", "EX", "10"], SYNTAX),
        (["EX", "10", "KEEPTTL"], SYNTAX),
        (["BOGUS"], SYNTAX),
        (["GET"], SYNTAX),
    ],
)
def test_bad_set_options(ctx, options, expected):
    assert run(ctx, "SET", "k", "v", *options) == expected
    assert run(ctx, "EXISTS", "k") == 0  # nothing was stored


def test_set_with_a_time_already_in_the_past_deletes_the_key(ctx, clock):
    run(ctx, "SET", "k", "old")

    assert run(ctx, "SET", "k", "new", "PXAT", clock.now - 1) == OK
    assert run(ctx, "EXISTS", "k") == 0
    assert run(ctx, "SET", "j", "new", "EXAT", 1) == OK
    assert run(ctx, "EXISTS", "j") == 0


# ---- EXPIRE and friends ---------------------------------------------------------------------


def test_expire_sets_a_time_to_live(ctx, clock):
    run(ctx, "SET", "k", "v")

    assert run(ctx, "EXPIRE", "k", 100) == 1
    assert run(ctx, "TTL", "k") == 100
    clock.advance(100_000)
    assert run(ctx, "GET", "k") is None


def test_expire_on_a_missing_key_returns_zero(ctx):
    assert run(ctx, "EXPIRE", "nothing", 10) == 0
    assert run(ctx, "PEXPIRE", "nothing", 10) == 0


def test_expire_replaces_an_earlier_expiry(ctx):
    run(ctx, "SET", "k", "v", "EX", 100)

    run(ctx, "EXPIRE", "k", 5)

    assert run(ctx, "TTL", "k") == 5


def test_pexpire_expireat_and_pexpireat(ctx, clock):
    run(ctx, "SET", "a", "v")
    run(ctx, "SET", "b", "v")
    run(ctx, "SET", "c", "v")

    assert run(ctx, "PEXPIRE", "a", 2500) == 1
    assert run(ctx, "EXPIREAT", "b", (clock.now + 30_000) // 1000) == 1
    assert run(ctx, "PEXPIREAT", "c", clock.now + 1234) == 1

    assert run(ctx, "PTTL", "a") == 2500
    assert run(ctx, "TTL", "b") == 30
    assert run(ctx, "PTTL", "c") == 1234


@pytest.mark.parametrize(
    ("command", "amount"),
    [("EXPIRE", 0), ("EXPIRE", -1), ("PEXPIRE", 0), ("PEXPIREAT", 1), ("EXPIREAT", 1)],
)
def test_a_zero_negative_or_past_time_deletes_the_key_and_replies_one(ctx, command, amount):
    run(ctx, "SET", "k", "v")

    assert run(ctx, command, "k", amount) == 1
    assert run(ctx, "EXISTS", "k") == 0


def test_expire_argument_errors(ctx):
    run(ctx, "SET", "k", "v")

    assert run(ctx, "EXPIRE", "k", "abc") == NOT_AN_INTEGER
    assert run(ctx, "EXPIRE", "k", "1.5") == NOT_AN_INTEGER
    assert run(ctx, "EXPIRE", "k", str(INT64_MAX)) == invalid_expire("expire")
    assert run(ctx, "PEXPIRE", "k", str(INT64_MAX)) == invalid_expire("pexpire")
    assert run(ctx, "EXPIRE", "k", 10, 11) == RespError("ERR Unsupported option 11")
    assert run(ctx, "EXPIRE", "k") == RespError(
        "ERR wrong number of arguments for 'expire' command"
    )
    assert run(ctx, "TTL", "k") == -1  # none of the errors changed anything


def test_expire_options_nx_xx_gt_lt(ctx):
    run(ctx, "SET", "k", "v")

    assert run(ctx, "EXPIRE", "k", 100, "XX") == 0  # XX: only if it already has an expiry
    assert run(ctx, "EXPIRE", "k", 100, "NX") == 1  # NX: only if it has none
    assert run(ctx, "EXPIRE", "k", 200, "NX") == 0
    assert run(ctx, "EXPIRE", "k", 50, "GT") == 0  # GT: only to a later time
    assert run(ctx, "EXPIRE", "k", 500, "GT") == 1
    assert run(ctx, "EXPIRE", "k", 5000, "LT") == 0  # LT: only to an earlier time
    assert run(ctx, "EXPIRE", "k", 50, "LT") == 1
    assert run(ctx, "TTL", "k") == 50


def test_expire_gt_and_lt_on_a_key_without_an_expiry(ctx):
    run(ctx, "SET", "k", "v")

    assert run(ctx, "EXPIRE", "k", 100, "GT") == 0  # no expiry counts as infinitely far away
    assert run(ctx, "TTL", "k") == -1
    assert run(ctx, "EXPIRE", "k", 100, "LT") == 1
    assert run(ctx, "TTL", "k") == 100


def test_expire_option_combinations(ctx):
    run(ctx, "SET", "k", "v", "EX", 100)
    incompatible = RespError(
        "ERR NX and XX, GT or LT options at the same time are not compatible"
    )

    assert run(ctx, "EXPIRE", "k", 100, "NX", "XX") == incompatible
    assert run(ctx, "EXPIRE", "k", 100, "nx", "gt") == incompatible
    assert run(ctx, "EXPIRE", "k", 100, "GT", "LT") == RespError(
        "ERR GT and LT options at the same time are not compatible"
    )
    assert run(ctx, "EXPIRE", "k", 100, "BOGUS") == RespError("ERR Unsupported option BOGUS")
    assert run(ctx, "EXPIRE", "k", 200, "xx", "gt") == 1
    assert run(ctx, "EXPIRE", "k", 100, "xx", "lt") == 1


def test_persist(ctx, clock):
    assert run(ctx, "PERSIST", "nothing") == 0
    run(ctx, "SET", "k", "v", "EX", 100)

    assert run(ctx, "PERSIST", "k") == 1
    assert run(ctx, "TTL", "k") == -1
    assert run(ctx, "PERSIST", "k") == 0
    clock.advance(1_000_000)
    assert run(ctx, "GET", "k") == b"v"


def test_ttl_and_pttl_of_missing_and_persistent_keys(ctx):
    assert run(ctx, "TTL", "nothing") == -2
    assert run(ctx, "PTTL", "nothing") == -2
    run(ctx, "SET", "k", "v")
    assert run(ctx, "TTL", "k") == -1
    assert run(ctx, "PTTL", "k") == -1


@pytest.mark.parametrize(("ms", "seconds"), [(1, 0), (499, 0), (500, 1), (1499, 1), (1500, 2)])
def test_ttl_rounds_to_the_nearest_second(ctx, ms, seconds):
    run(ctx, "SET", "k", "v", "PX", ms)

    assert run(ctx, "TTL", "k") == seconds

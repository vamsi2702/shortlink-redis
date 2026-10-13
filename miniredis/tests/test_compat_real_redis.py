"""Differential tests: the same commands go to MiniRedis and to real Redis, and the replies must be
identical. Skipped automatically if real Redis is not running (docker compose up -d redis).

Every key is prefixed with a unique string and removed afterwards, so nothing else on that Redis
is touched.
"""

import uuid

import pytest

from miniredis.resp import OK, encode_command

LONG = "x" * 60

# Each scenario is a list of commands run in order on ONE fresh connection per server.
# "{p}" is replaced by the unique key prefix.
SCENARIOS = {
    "ping": [["PING"], ["PING", "hi"], ["PING", ""], ["PING", "a", "b"], ["ping"]],
    "echo": [["ECHO", "hi"], ["ECHO", ""], ["ECHO"], ["ECHO", "a", "b"]],
    "set and get": [
        ["SET", "{p}a", "1"],
        ["GET", "{p}a"],
        ["GET", "{p}missing"],
        ["SET", "{p}a", "2"],
        ["GET", "{p}a"],
        ["SET", "{p}empty", ""],
        ["GET", "{p}empty"],
        ["EXISTS", "{p}empty"],
    ],
    "set and get errors": [
        ["GET"],
        ["GeT", "{p}a", "x"],
        ["SET"],
        ["SET", "{p}a"],
        ["SET", "{p}a", "v", "bogus"],
    ],
    "unknown commands": [
        ["FOO"],
        ["FoO", "a", "b"],
        ["foo", "a\r\nb", "c"],
        ["foo", LONG, LONG, LONG, LONG, LONG],
        ["foo", "x", LONG, LONG, LONG],
    ],
    "del and exists": [
        ["SET", "{p}a", "1"],
        ["SET", "{p}b", "2"],
        ["EXISTS", "{p}a", "{p}a", "{p}nope"],
        ["DEL", "{p}a", "{p}a"],
        ["DEL", "{p}b", "{p}nope"],
        ["DEL", "{p}a"],
        ["DEL"],
        ["EXISTS"],
    ],
    "incr basics": [
        ["INCR", "{p}n"],
        ["INCR", "{p}n"],
        ["GET", "{p}n"],
        ["INCRBY", "{p}n", "10"],
        ["INCRBY", "{p}n", "-3"],
        ["INCRBY", "{p}n", "0"],
        ["INCR"],
        ["INCR", "{p}n", "x"],
        ["INCRBY", "{p}n"],
    ],
    "incr on bad stored values": [
        step
        for stored in ["05", " 5", "+5", "5 ", "", "-0", "abc", "1.5", "0x10", "1e3"]
        for step in (["SET", "{p}n", stored], ["INCR", "{p}n"], ["GET", "{p}n"])
    ],
    "incr limits": [
        ["SET", "{p}n", "9223372036854775807"],
        ["INCR", "{p}n"],
        ["SET", "{p}n", "-9223372036854775808"],
        ["INCR", "{p}n"],
        ["SET", "{p}m", "9223372036854775806"],
        ["INCRBY", "{p}m", "1"],
        ["INCRBY", "{p}m", "1"],
        ["INCRBY", "{p}z", "-9223372036854775808"],
        ["INCRBY", "{p}z", "-1"],
    ],
    "incrby bad amounts": [
        ["INCRBY", "{p}n", amount]
        for amount in ["1.5", "abc", "", "9223372036854775808", " 1", "+1", "007", "-0"]
    ],
    "client": [
        ["CLIENT"],
        ["CLIENT", "BOGUS"],
        ["CLIENT", "SETNAME"],
        ["CLIENT", "SETNAME", "has space"],
        ["CLIENT", "SETNAME", "tab\there"],
        ["CLIENT", "GETNAME"],
        ["CLIENT", "SETNAME", "mini"],
        ["CLIENT", "GETNAME"],
        ["CLIENT", "SETNAME", ""],
        ["CLIENT", "GETNAME"],
        ["client", "setname", "lower"],
        ["CLIENT", "SETINFO", "lib-name", "x"],
        ["CLIENT", "SETINFO", "LIB-VER", "1.0"],
        ["CLIENT", "SETINFO", "foo", "bar"],
        ["CLIENT", "SETINFO", "lib-name"],
        ["CLIENT", "SETINFO", "LIB-NAME", "a b"],
        ["CLIENT", "MAINT_NOTIFICATIONS", "ON"],
        ["CLIENT", "GETNAME", "extra"],
    ],
    "hello errors": [
        ["HELLO", "x"],
        ["HELLO", "1"],
        ["HELLO", "0"],
        ["HELLO", "4"],
        ["HELLO", "2", "foo"],
        ["HELLO", "2", "SETNAME", "bad name"],
    ],
    "command": [["COMMAND", "BOGUS"]],
}


def run_scenario(client, scenario, prefix):
    replies = []
    for step in scenario:
        words = [word.replace("{p}", prefix) for word in step]
        client.send_raw(encode_command(*words))
        replies.append(client.read(timeout=5))
    return replies


@pytest.mark.parametrize("name", SCENARIOS)
def test_replies_match_real_redis(name, connect, connect_real):
    scenario = SCENARIOS[name]
    prefix = f"compat-{uuid.uuid4().hex[:8]}:"
    mini, real = connect(), connect_real()
    try:
        ours = run_scenario(mini, scenario, prefix)
        theirs = run_scenario(real, scenario, prefix)
    finally:
        keys = {w.replace("{p}", prefix) for step in scenario for w in step if "{p}" in w}
        if keys:
            real.call("DEL", *keys)

    for step, our_reply, real_reply in zip(scenario, ours, theirs, strict=True):
        message = f"{step}: MiniRedis said {our_reply!r}, real Redis said {real_reply!r}"
        assert our_reply == real_reply, message


def test_hello_2_has_the_same_shape_as_real_redis(connect, connect_real):
    ours = connect().call("HELLO", "2")
    theirs = connect_real().call("HELLO", "2")

    assert isinstance(ours, list) and isinstance(theirs, list)
    assert len(ours) == len(theirs) == 14
    assert ours[::2] == theirs[::2]  # the same field names in the same order
    assert [type(v) for v in ours[1::2]] == [type(v) for v in theirs[1::2]]
    assert ours[ours.index(b"proto") + 1] == theirs[theirs.index(b"proto") + 1] == 2


def test_quit_replies_ok_and_then_both_servers_hang_up(connect, connect_real):
    for client in (connect(), connect_real()):
        assert client.call("QUIT") == OK
        assert client.server_closed_connection()

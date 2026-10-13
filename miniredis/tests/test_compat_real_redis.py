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
    "set with expiry": [
        ["SET", "{p}k", "v", "EX", "100"],
        ["TTL", "{p}k"],
        ["SET", "{p}k", "v", "PX", "100000"],
        ["TTL", "{p}k"],
        ["SET", "{p}k", "v2"],
        ["TTL", "{p}k"],
        ["SET", "{p}k", "v3", "EX", "50"],
        ["SET", "{p}k", "v4", "KEEPTTL"],
        ["TTL", "{p}k"],
        ["GET", "{p}k"],
        ["DEL", "{p}k"],
        ["SET", "{p}k", "v", "NX"],
        ["SET", "{p}k", "v", "NX"],
        ["SET", "{p}k", "w", "XX"],
        ["DEL", "{p}k"],
        ["SET", "{p}k", "w", "XX"],
        ["GET", "{p}k"],
        ["SET", "{p}k", "v", "nx", "ex", "10"],
        ["TTL", "{p}k"],
        ["SET", "{p}k", "v", "NX", "NX"],
        ["SET", "{p}k", "v", "ex", "5", "nx"],
        ["SET", "{p}k", "v", "EX", "10", "EX", "20"],
        ["TTL", "{p}k"],
    ],
    "set option errors": [
        ["SET", "{p}k", "v", *options]
        for options in (
            ["EX"], ["EX", "abc"], ["EX", "0"], ["EX", "-5"], ["PX", "0"],
            ["EX", "9223372036854775807"], ["PX", "9223372036854775807"], ["EXAT", "0"],
            ["EX", "1.5"], ["EX", "10", "PX", "10"], ["NX", "XX"], ["XX", "NX"],
            ["KEEPTTL", "EX", "10"], ["EX", "10", "KEEPTTL"], ["BOGUS"], ["EX", " 10"],
            ["ex", "+10"],
        )
    ],  # fmt: skip
    "set in the past": [
        ["SET", "{p}k", "old"],
        ["SET", "{p}k", "new", "PXAT", "1"],
        ["GET", "{p}k"],
        ["EXISTS", "{p}k"],
        ["SET", "{p}k", "old"],
        ["SET", "{p}k", "new", "EXAT", "1"],
        ["GET", "{p}k"],
    ],
    "ttl persist and expire": [
        ["TTL", "{p}k"],
        ["PTTL", "{p}k"],
        ["PERSIST", "{p}k"],
        ["EXPIRE", "{p}k", "10"],
        ["SET", "{p}k", "v"],
        ["TTL", "{p}k"],
        ["PTTL", "{p}k"],
        ["PERSIST", "{p}k"],
        ["EXPIRE", "{p}k", "100"],
        ["TTL", "{p}k"],
        ["PERSIST", "{p}k"],
        ["TTL", "{p}k"],
        ["PERSIST", "{p}k"],
        ["EXPIRE", "{p}k", "abc"],
        ["EXPIRE", "{p}k", "1.5"],
        ["EXPIRE", "{p}k"],
        ["EXPIRE", "{p}k", "10", "11"],
        ["EXPIRE", "{p}k", "9223372036854775807"],
        ["PEXPIRE", "{p}k", "9223372036854775807"],
        ["EXPIRE", "{p}k", "0"],
        ["GET", "{p}k"],
        ["SET", "{p}k", "v"],
        ["EXPIRE", "{p}k", "-1"],
        ["EXISTS", "{p}k"],
        ["SET", "{p}k", "v"],
        ["PEXPIREAT", "{p}k", "1"],
        ["EXISTS", "{p}k"],
        ["SET", "{p}k", "v"],
        ["EXPIREAT", "{p}k", "1"],
        ["EXISTS", "{p}k"],
        ["TTL"],
        ["PTTL", "a", "b"],
        ["PERSIST"],
    ],
    "expire options": [
        ["SET", "{p}k", "v"],
        ["EXPIRE", "{p}k", "100", "XX"],
        ["EXPIRE", "{p}k", "100", "NX"],
        ["EXPIRE", "{p}k", "200", "NX"],
        ["EXPIRE", "{p}k", "50", "GT"],
        ["EXPIRE", "{p}k", "500", "GT"],
        ["EXPIRE", "{p}k", "5000", "LT"],
        ["EXPIRE", "{p}k", "50", "LT"],
        ["TTL", "{p}k"],
        ["PERSIST", "{p}k"],
        ["EXPIRE", "{p}k", "100", "GT"],
        ["EXPIRE", "{p}k", "100", "LT"],
        ["TTL", "{p}k"],
        ["EXPIRE", "{p}k", "100", "NX", "XX"],
        ["EXPIRE", "{p}k", "100", "GT", "LT"],
        ["EXPIRE", "{p}k", "100", "BOGUS"],
        ["EXPIRE", "{p}k", "100", "nx", "gt"],
        ["EXPIRE", "{p}k", "100", "xx", "gt"],
        ["EXPIRE", "{p}k", "100", "xx", "lt"],
    ],
    "lists": [
        ["LLEN", "{p}l"],
        ["LPUSH", "{p}l", "a"],
        ["LPUSH", "{p}l", "b", "c"],
        ["LLEN", "{p}l"],
        ["LRANGE", "{p}l", "0", "-1"],
        ["TYPE", "{p}l"],
        ["RPOP", "{p}l"],
        ["RPOP", "{p}l"],
        ["RPOP", "{p}l"],
        ["RPOP", "{p}l"],
        ["EXISTS", "{p}l"],
        ["TYPE", "{p}l"],
        ["LPUSH", "{p}l", ""],
        ["LRANGE", "{p}l", "0", "-1"],
        ["RPOP", "{p}l", "9223372036854775807"],
    ],
    "rpop with a count": [
        ["LPUSH", "{p}l", "1", "2", "3", "4", "5"],
        ["RPOP", "{p}l", "2"],
        ["RPOP", "{p}l", "0"],
        ["RPOP", "{p}l", "100"],
        ["EXISTS", "{p}l"],
        ["RPOP", "{p}l", "2"],
        ["RPOP", "{p}nokey", "0"],
        ["LPUSH", "{p}l", "x"],
        ["RPOP", "{p}l", "-1"],
        ["RPOP", "{p}l", "abc"],
        ["RPOP", "{p}l", "1.5"],
        ["RPOP", "{p}l", "+1"],
        ["RPOP", "{p}l", "1", "2"],
        ["RPOP"],
        ["RPOP", "{p}l", "1"],
        ["RPOP", "{p}l", "1"],
    ],
    "lrange": [
        ["LPUSH", "{p}l", "e", "d", "c", "b", "a"],
        *(
            ["LRANGE", "{p}l", start, stop]
            for start, stop in [
                ("0", "-1"), ("1", "2"), ("-2", "-1"), ("-100", "100"), ("3", "1"), ("10", "20"),
                ("0", "0"), ("-1", "-1"), ("4", "100"), ("5", "100"), ("0", "-100"),
                ("9223372036854775807", "9223372036854775807"),
                ("-9223372036854775808", "-1"), ("a", "1"), ("0", "1.5"), ("", "1"),
            ]
        ),
        ["LRANGE", "{p}nokey", "0", "-1"],
        ["LRANGE", "{p}l", "0"],
        ["LRANGE", "{p}l", "0", "1", "2"],
    ],  # fmt: skip
    "list arity": [
        ["LPUSH"],
        ["LPUSH", "{p}l"],
        ["LLEN"],
        ["LLEN", "{p}l", "x"],
        ["TYPE"],
        ["TYPE", "a", "b"],
    ],
    "list and string types": [
        ["SET", "{p}s", "v"],
        ["LLEN", "{p}s"],
        ["LPUSH", "{p}s", "x"],
        ["RPOP", "{p}s"],
        ["RPOP", "{p}s", "2"],
        ["LRANGE", "{p}s", "0", "-1"],
        ["LPUSH", "{p}l", "x"],
        ["GET", "{p}l"],
        ["INCR", "{p}l"],
        ["INCRBY", "{p}l", "5"],
        ["TYPE", "{p}s"],
        ["TYPE", "{p}l"],
        ["TYPE", "{p}nothing"],
        ["SET", "{p}l", "overwrites"],
        ["TYPE", "{p}l"],
        ["GET", "{p}l"],
    ],
    "lists and expiry": [
        ["LPUSH", "{p}l", "x"],
        ["EXPIRE", "{p}l", "100"],
        ["TTL", "{p}l"],
        ["LPUSH", "{p}l", "y"],
        ["TTL", "{p}l"],
        ["RPOP", "{p}l", "5"],
        ["EXISTS", "{p}l"],
        ["TTL", "{p}l"],
        ["LPUSH", "{p}l", "a", "b"],
        ["DEL", "{p}l"],
        ["EXISTS", "{p}l"],
    ],
    "hashes": [
        ["HSET", "{p}h", "f", "v"],
        ["HGET", "{p}h", "f"],
        ["HGET", "{p}h", "nofield"],
        ["HGET", "{p}nokey", "f"],
        ["HSET", "{p}h", "f", "v2"],
        ["HGET", "{p}h", "f"],
        ["HSET", "{p}h", "g", "1", "h", "2"],
        ["HSET", "{p}h", "g", "9", "i", "3"],
        ["HGETALL", "{p}h"],
        ["HGETALL", "{p}nokey"],
        ["TYPE", "{p}h"],
        ["HSET", "{p}h", "dup", "1", "dup", "2"],
        ["HGET", "{p}h", "dup"],
        ["HSET", "{p}h", "", ""],
        ["HGET", "{p}h", ""],
        ["HSET", "{p}h", "apple", "A", "banana", "B"],
        ["HSET", "{p}h", "apple", "CHANGED"],
        ["HGETALL", "{p}h"],
    ],
    "hash arity": [
        ["HSET"],
        ["HSET", "{p}h"],
        ["HSET", "{p}h", "f"],
        ["HSET", "{p}h", "f", "v", "g"],
        ["HGET"],
        ["HGET", "{p}h"],
        ["HGET", "{p}h", "f", "x"],
        ["HGETALL"],
        ["HGETALL", "{p}h", "x"],
    ],
    "hashes and other types": [
        ["SET", "{p}s", "v"],
        ["LPUSH", "{p}l", "x"],
        ["HSET", "{p}h", "f", "v"],
        ["HSET", "{p}s", "f", "v"],
        ["HGET", "{p}s", "f"],
        ["HGETALL", "{p}s"],
        ["HSET", "{p}l", "f", "v"],
        ["HGET", "{p}l", "f"],
        ["HGETALL", "{p}l"],
        ["GET", "{p}h"],
        ["INCR", "{p}h"],
        ["LLEN", "{p}h"],
        ["LPUSH", "{p}h", "x"],
        ["RPOP", "{p}h"],
        ["LRANGE", "{p}h", "0", "-1"],
        ["SET", "{p}h", "now-a-string"],
        ["TYPE", "{p}h"],
    ],
    "hashes and expiry": [
        ["HSET", "{p}h", "f", "v"],
        ["EXPIRE", "{p}h", "100"],
        ["TTL", "{p}h"],
        ["HSET", "{p}h", "g", "w"],
        ["TTL", "{p}h"],
        ["DEL", "{p}h"],
        ["EXISTS", "{p}h"],
        ["HGETALL", "{p}h"],
    ],
    "incr keeps the expiry": [
        ["SET", "{p}k", "5", "EX", "100"],
        ["INCR", "{p}k"],
        ["TTL", "{p}k"],
        ["SET", "{p}k", "9"],
        ["TTL", "{p}k"],
        ["SET", "{p}k", "1", "EX", "100"],
        ["INCRBY", "{p}k", "5"],
        ["TTL", "{p}k"],
    ],
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


def raw_reply(client, *words) -> bytes:
    """The exact bytes of the server's answer, without parsing them."""
    client.sock.sendall(encode_command(*words))
    client.sock.settimeout(2)
    return client.sock.recv(65536)


@pytest.mark.parametrize(
    "words",
    [
        ("RPOP", "{p}missing"),
        ("RPOP", "{p}missing", "2"),
        ("RPOP", "{p}missing", "0"),
        ("LRANGE", "{p}missing", "0", "-1"),
        ("GET", "{p}missing"),
        ("LLEN", "{p}missing"),
    ],
)
def test_nulls_and_empty_arrays_are_the_same_bytes_as_real_redis(words, connect, connect_real):
    prefix = f"compat-{uuid.uuid4().hex[:8]}:"
    words = [word.replace("{p}", prefix) for word in words]

    assert raw_reply(connect(), *words) == raw_reply(connect_real(), *words)


def test_quit_replies_ok_and_then_both_servers_hang_up(connect, connect_real):
    for client in (connect(), connect_real()):
        assert client.call("QUIT") == OK
        assert client.server_closed_connection()

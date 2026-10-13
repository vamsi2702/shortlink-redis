"""Helpers shared by the persistence tests."""

import random
from collections import deque

from miniredis import commands
from miniredis.aof import replay
from miniredis.commands import ClientState, Context, execute
from miniredis.resp import encode_command
from miniredis.store import Store


class RecordingLog:
    def __init__(self):
        self.commands: list[list[bytes]] = []

    def append(self, words):
        self.commands.append(words)


def dump(store):
    """Everything live in a store, in a form that can be compared."""
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


def replay_into(store, path):
    """Replay a file into a store the way the server does."""
    ctx = Context(store=store, client=ClientState(id=0))
    store.loading = True
    try:
        return replay(path, lambda words: commands.execute(ctx, words))
    finally:
        store.loading = False


def write_log(path, *command_list):
    path.write_bytes(b"".join(encode_command(*words) for words in command_list))


KEYS = [b"a", b"b", b"c", b"d"]


def random_command(rng):
    key = rng.choice(KEYS)
    kind = rng.choice(
        ["set", "set_ex", "set_flag", "get", "del", "incr", "incrby", "expire", "pexpire",
         "expireat", "persist", "lpush", "rpop", "hset", "ttl", "type"]
    )  # fmt: skip
    number = str(rng.randrange(0, 12))
    if kind == "set":
        return [b"SET", key, rng.choice([b"1", b"7", b"text", b""])]
    if kind == "set_ex":
        option = rng.choice([b"EX", b"PX", b"EXAT", b"PXAT"])
        return [b"SET", key, b"5", option, str(rng.randrange(1, 9)).encode()]
    if kind == "set_flag":
        return [b"SET", key, b"9", rng.choice([b"NX", b"XX", b"KEEPTTL"])]
    if kind == "get":
        return [b"GET", key]
    if kind == "del":
        return [b"DEL", key, rng.choice(KEYS)]
    if kind == "incr":
        return [b"INCR", key]
    if kind == "incrby":
        return [b"INCRBY", key, str(rng.randrange(-5, 6)).encode()]
    if kind == "expire":
        option = rng.choice([[], [b"GT"], [b"NX"]])
        return [b"EXPIRE", key, str(rng.randrange(-1, 6)).encode(), *option]
    if kind == "pexpire":
        return [b"PEXPIRE", key, str(rng.randrange(0, 4000)).encode()]
    if kind == "expireat":
        return [b"PEXPIREAT", key, b"1"]  # the distant past: deletes the key
    if kind == "persist":
        return [b"PERSIST", key]
    if kind == "lpush":
        return [b"LPUSH", key, number.encode(), b"x"]
    if kind == "rpop":
        return [b"RPOP", key, *rng.choice([[], [b"2"], [b"100"]])]
    if kind == "hset":
        return [b"HSET", key, rng.choice([b"f", b"g"]), number.encode()]
    if kind == "ttl":
        return [b"TTL", key]
    return [b"TYPE", key]


def random_session(seed, clock, steps=250):
    """Run random commands (and let time pass) on a store that logs like the real server."""
    rng = random.Random(seed)
    log = RecordingLog()
    store = Store(clock=clock)
    store.on_expire = lambda key: log.append([b"DEL", key])
    ctx = Context(store=store, client=ClientState(id=1), aof=log)
    for _ in range(steps):
        if rng.random() < 0.4:
            clock.advance(rng.choice([0, 100, 700, 1500, 3000, 6000]))
        if rng.random() < 0.1:
            store.purge_expired()  # the background cleaner
        execute(ctx, random_command(rng))
    return store, log.commands

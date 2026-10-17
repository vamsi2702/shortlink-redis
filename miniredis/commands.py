"""Commands: the table that maps a command name to the code that runs it.

`execute(ctx, words)` takes a parsed command such as [b"SET", b"a", b"1"] and returns a reply
value that miniredis.resp can encode.

RULE: a command handler must never `await`. The server is one thread running an event loop, so a
handler that never awaits cannot be interrupted, which is what makes every command atomic (INCR
cannot lose a count) without any locks. `test_commands_basic.py` checks that no handler is async.
"""

import logging
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import islice

from miniredis.info import render as render_info
from miniredis.pubsub import PubSub
from miniredis.resp import NULL_ARRAY, OK, PONG, RespError, SimpleString
from miniredis.store import Store

log = logging.getLogger("miniredis.commands")

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
# Redis is strict: no plus sign, no spaces, no leading zeros, and "-0" is not a number
_INTEGER = re.compile(rb"0|-?[1-9][0-9]{0,18}")
NOT_AN_INTEGER = "ERR value is not an integer or out of range"
WRONGTYPE = "WRONGTYPE Operation against a key holding the wrong kind of value"


@dataclass(eq=False)  # eq=False: two clients are the same only if they are the very same object
class ClientState:
    """What the server remembers about one connected client."""

    id: int
    name: bytes | None = None
    lib_name: bytes | None = None
    lib_ver: bytes | None = None
    close_after_reply: bool = False
    subscriptions: dict[bytes, None] = field(default_factory=dict)  # channels it listens to
    # sends bytes to this client out of turn, for published messages; set by the server
    push: Callable[[bytes], bool] | None = None


class Replies(list):
    """A handler returns this when the answer to ONE command is SEVERAL separate RESP messages,
    as SUBSCRIBE does (one confirmation per channel). Each item is encoded on its own."""


@dataclass
class Stats:
    """Counters for the whole server, shown by INFO. One is shared by every client."""

    started_at: float = field(default_factory=time.time)
    connections_received: int = 0
    connected_clients: int = 0
    commands_processed: int = 0
    keyspace_hits: int = 0
    keyspace_misses: int = 0


@dataclass
class Context:
    """Everything a command handler may use."""

    store: Store
    client: ClientState
    stats: Stats = field(default_factory=Stats)
    # The server's own facts for INFO (such as its listening port), by section name.
    extra_info: Callable[[], dict[str, dict[str, object]]] | None = None
    pubsub: PubSub = field(default_factory=PubSub)  # shared by all clients


class CommandError(Exception):
    """Raised by a handler to reply with an error. The text starts with its kind, e.g. 'ERR ...'."""


Handler = Callable[[Context, list[bytes]], object]


@dataclass(frozen=True)
class Command:
    name: str  # lowercase, as shown in error messages
    arity: int  # number of words including the name; negative means "at least"
    handler: Handler


COMMANDS: dict[bytes, Command] = {}

# A client that has subscribed to a channel is listening: only these commands are allowed then.
_ALLOWED_WHEN_SUBSCRIBED = {"subscribe", "unsubscribe", "ping", "quit"}


def command(name: str, arity: int) -> Callable[[Handler], Handler]:
    """Register a handler. `arity` counts the command name too, as Redis does: GET is 2."""

    def register(handler: Handler) -> Handler:
        COMMANDS[name.upper().encode()] = Command(name.lower(), arity, handler)
        return handler

    return register


def parse_int64(raw: bytes) -> int | None:
    """The integer in `raw` if it is written exactly the way Redis accepts, else None."""
    if not _INTEGER.fullmatch(raw):
        return None
    value = int(raw)
    return value if INT64_MIN <= value <= INT64_MAX else None


def note_lookup(ctx: Context, found: bool) -> None:
    """Count a key lookup by a read command as a hit or a miss (shown by INFO)."""
    if found:
        ctx.stats.keyspace_hits += 1
    else:
        ctx.stats.keyspace_misses += 1


def safe_text(raw: bytes) -> str:
    """Bytes as text that is safe to put in an error reply (no CR or LF, which would end it)."""
    return raw.decode("utf-8", "replace").replace("\r", " ").replace("\n", " ")


def wrong_arity(name: str) -> CommandError:
    return CommandError(f"ERR wrong number of arguments for '{name}' command")


def execute(ctx: Context, words: list[bytes]) -> object:
    """Run one command and return its reply. Never raises: problems become error replies."""
    spec = COMMANDS.get(words[0].upper())
    if spec is None:
        return _unknown_command(words)
    count = len(words)
    if (spec.arity >= 0 and count != spec.arity) or (spec.arity < 0 and count < -spec.arity):
        return RespError(str(wrong_arity(spec.name)))
    if ctx.client.subscriptions and spec.name not in _ALLOWED_WHEN_SUBSCRIBED:
        return RespError(
            f"ERR Can't execute '{spec.name}': only (P|S)SUBSCRIBE / (P|S)UNSUBSCRIBE / "
            "PING / QUIT / RESET are allowed in this context"
        )
    try:
        return spec.handler(ctx, words[1:])
    except CommandError as exc:
        return RespError(str(exc))
    except Exception:  # a bug in one command must not take the whole server down
        log.exception("command %s crashed", spec.name)
        return RespError("ERR internal error")
    finally:
        ctx.stats.commands_processed += 1  # counted after it ran, so INFO does not count itself


def _unknown_command(words: list[bytes]) -> RespError:
    # Real Redis echoes at most 128 bytes of arguments in total: it keeps adding arguments while
    # fewer than 128 bytes are shown, cutting the last one short. We copy that exactly.
    shown = b""
    for arg in words[1:]:
        if len(shown) >= 128:
            break
        shown += b"'" + arg[: 128 - len(shown)] + b"' "
    return RespError(
        f"ERR unknown command '{safe_text(words[0][:128])}', "
        f"with args beginning with: {safe_text(shown)}"
    )


# ---- connection and utility commands --------------------------------------------------------


@command("ping", -1)
def ping(ctx: Context, args: list[bytes]) -> object:
    if len(args) > 1:
        raise wrong_arity("ping")
    if ctx.client.subscriptions:  # a subscribed client gets an array, so it can tell it apart
        return [b"pong", args[0] if args else b""]
    return args[0] if args else PONG


@command("echo", 2)
def echo(ctx: Context, args: list[bytes]) -> object:
    return args[0]


# ---- strings --------------------------------------------------------------------------------


def get_string(ctx: Context, key: bytes) -> bytes | None:
    """The string stored at `key`, None if there is none, or a WRONGTYPE error for other types."""
    value = ctx.store.get(key)
    if value is not None and not isinstance(value, bytes):
        raise CommandError(WRONGTYPE)
    return value


# SET option -> (milliseconds per unit, is it an absolute time of day?)
_EXPIRY_OPTIONS = {
    b"EX": (1000, False),
    b"PX": (1, False),
    b"EXAT": (1000, True),
    b"PXAT": (1, True),
}


def expiry_time(ctx: Context, amount: int, unit_ms: int, absolute: bool, command_name: str) -> int:
    """Turn an amount of seconds or milliseconds into a unix time in ms, refusing nonsense."""
    invalid = CommandError(f"ERR invalid expire time in '{command_name}' command")
    if command_name == "set" and amount <= 0:
        raise invalid  # SET refuses zero and negative times; EXPIRE accepts them (they delete)
    if amount > INT64_MAX // unit_ms or amount < INT64_MIN // unit_ms:
        raise invalid
    when = amount * unit_ms
    if not absolute:
        when += ctx.store.now()
    if when > INT64_MAX:
        raise invalid
    return when


@dataclass
class SetOptions:
    nx: bool = False
    xx: bool = False
    keep_ttl: bool = False
    expire_at: int | None = None


def parse_set_options(ctx: Context, args: list[bytes]) -> SetOptions:
    """NX | XX, KEEPTTL, and one of EX | PX | EXAT | PXAT, with the same conflicts Redis has."""
    options = SetOptions()
    seen: set[bytes] = set()  # which expiry options appeared
    i = 0
    while i < len(args):
        option = args[i].upper()
        has_value = i + 1 < len(args)
        if option == b"NX" and not options.xx:
            options.nx = True
        elif option == b"XX" and not options.nx:
            options.xx = True
        elif option == b"KEEPTTL" and not seen:
            options.keep_ttl = True
        elif option in _EXPIRY_OPTIONS and has_value and not options.keep_ttl and not (
            seen - {option}
        ):
            amount = parse_int64(args[i + 1])
            if amount is None:
                raise CommandError(NOT_AN_INTEGER)
            unit_ms, absolute = _EXPIRY_OPTIONS[option]
            options.expire_at = expiry_time(ctx, amount, unit_ms, absolute, "set")
            seen.add(option)
            i += 1
        else:
            raise CommandError("ERR syntax error")
        i += 1
    return options


@command("set", -3)
def set_(ctx: Context, args: list[bytes]) -> object:
    key, value = args[0], args[1]
    options = parse_set_options(ctx, args[2:])
    store = ctx.store
    exists = store.exists(key)
    if (options.nx and exists) or (options.xx and not exists):
        return None  # the condition failed: nothing is changed and the reply is a null
    if options.expire_at is not None and options.expire_at <= store.now():
        store.delete(key)  # an expiry in the past means the key is gone straight away
        return OK
    store.set(key, value, expire_at=options.expire_at, keep_ttl=options.keep_ttl)
    return OK


@command("get", 2)
def get(ctx: Context, args: list[bytes]) -> object:
    value = get_string(ctx, args[0])
    note_lookup(ctx, value is not None)
    return value  # None becomes a RESP null


@command("del", -2)
def del_(ctx: Context, args: list[bytes]) -> object:
    """Delete keys; the reply is how many actually existed (DEL a a counts a only once)."""
    return sum(ctx.store.delete(key) for key in args)


@command("exists", -2)
def exists(ctx: Context, args: list[bytes]) -> object:
    """How many of the keys exist; a key listed twice is counted twice."""
    present = 0
    for key in args:
        found = ctx.store.exists(key)
        note_lookup(ctx, found)
        present += found
    return present


def _increment(ctx: Context, key: bytes, amount: int) -> int:
    current = get_string(ctx, key)
    if current is None:
        value = 0  # a missing key counts as 0
    else:
        parsed = parse_int64(current)
        if parsed is None:
            raise CommandError(NOT_AN_INTEGER)
        value = parsed
    result = value + amount
    if not INT64_MIN <= result <= INT64_MAX:
        raise CommandError("ERR increment or decrement would overflow")
    ctx.store.set(key, str(result).encode(), keep_ttl=True)  # counting does not reset the expiry
    return result


@command("incr", 2)
def incr(ctx: Context, args: list[bytes]) -> object:
    return _increment(ctx, args[0], 1)


@command("incrby", 3)
def incrby(ctx: Context, args: list[bytes]) -> object:
    # redis-py sends INCRBY key 1 even for a plain incr() (seen with proxy_sniffer.py)
    amount = parse_int64(args[1])
    if amount is None:
        raise CommandError(NOT_AN_INTEGER)
    return _increment(ctx, args[0], amount)


# ---- type and lists ---------------------------------------------------------------------------


def type_name(value: object | None) -> str:
    if value is None:
        return "none"
    if isinstance(value, bytes):
        return "string"
    if isinstance(value, deque):
        return "list"
    if isinstance(value, dict):
        return "hash"
    raise TypeError(f"unknown value type {type(value).__name__}")


@command("type", 2)
def type_(ctx: Context, args: list[bytes]) -> object:
    value = ctx.store.get(args[0])
    note_lookup(ctx, value is not None)
    return SimpleString(type_name(value))


def get_list(ctx: Context, key: bytes, *, create: bool = False) -> deque[bytes] | None:
    """The list at `key`, None if there is none (or a new empty one with create=True), or a
    WRONGTYPE error if the key holds something else."""
    value = ctx.store.get(key)
    if value is None:
        if not create:
            return None
        value = deque()
        ctx.store.set(key, value)
        return value
    if not isinstance(value, deque):
        raise CommandError(WRONGTYPE)
    return value


def drop_if_empty(ctx: Context, key: bytes, value: object) -> None:
    """Redis never keeps an empty list or hash around: when the last item goes, the key goes."""
    if not value:
        ctx.store.delete(key)


@command("lpush", -3)
def lpush(ctx: Context, args: list[bytes]) -> object:
    items = get_list(ctx, args[0], create=True)
    assert items is not None
    for value in args[1:]:
        items.appendleft(value)  # each value goes in front of the previous one
    return len(items)


@command("rpop", -2)
def rpop(ctx: Context, args: list[bytes]) -> object:
    if len(args) > 2:
        raise wrong_arity("rpop")
    count: int | None = None
    if len(args) == 2:
        count = parse_int64(args[1])
        if count is None or count < 0:
            raise CommandError("ERR value is out of range, must be positive")
    items = get_list(ctx, args[0])
    if items is None:
        return None if count is None else NULL_ARRAY
    if count is None:
        value = items.pop()
        drop_if_empty(ctx, args[0], items)
        return value
    popped = [items.pop() for _ in range(min(count, len(items)))]
    drop_if_empty(ctx, args[0], items)
    return popped


@command("llen", 2)
def llen(ctx: Context, args: list[bytes]) -> object:
    items = get_list(ctx, args[0])
    note_lookup(ctx, items is not None)
    return 0 if items is None else len(items)


@command("lrange", 4)
def lrange(ctx: Context, args: list[bytes]) -> object:
    start, stop = parse_int64(args[1]), parse_int64(args[2])
    if start is None or stop is None:
        raise CommandError(NOT_AN_INTEGER)
    items = get_list(ctx, args[0])
    note_lookup(ctx, items is not None)
    if items is None:
        return []
    size = len(items)
    if start < 0:  # negative positions count from the end: -1 is the last item
        start = max(size + start, 0)
    if stop < 0:
        stop += size
    if start > stop or start >= size:
        return []
    return list(islice(items, start, min(stop, size - 1) + 1))


# ---- hashes -----------------------------------------------------------------------------------


def get_hash(ctx: Context, key: bytes, *, create: bool = False) -> dict[bytes, bytes] | None:
    """The hash at `key`, None if there is none (or a new empty one with create=True), or a
    WRONGTYPE error if the key holds something else."""
    value = ctx.store.get(key)
    if value is None:
        if not create:
            return None
        value = {}
        ctx.store.set(key, value)
        return value
    if not isinstance(value, dict):
        raise CommandError(WRONGTYPE)
    return value


@command("hset", -4)
def hset(ctx: Context, args: list[bytes]) -> object:
    """Set one or more fields. The reply is how many fields are NEW (updates do not count)."""
    if len(args) % 2 == 0:  # the key, then field/value pairs: the count must be odd
        raise wrong_arity("hset")
    fields = get_hash(ctx, args[0], create=True)
    assert fields is not None
    added = 0
    for i in range(1, len(args), 2):
        if args[i] not in fields:
            added += 1
        fields[args[i]] = args[i + 1]
    return added


@command("hget", 3)
def hget(ctx: Context, args: list[bytes]) -> object:
    fields = get_hash(ctx, args[0])
    note_lookup(ctx, fields is not None)
    return None if fields is None else fields.get(args[1])


@command("hgetall", 2)
def hgetall(ctx: Context, args: list[bytes]) -> object:
    """Every field and value as one flat list: field, value, field, value, ..."""
    fields = get_hash(ctx, args[0])
    note_lookup(ctx, fields is not None)
    if fields is None:
        return []
    return [item for pair in fields.items() for item in pair]


# ---- expiry -----------------------------------------------------------------------------------


def _expire_command(
    ctx: Context, args: list[bytes], name: str, unit_ms: int, absolute: bool
) -> object:
    key = args[0]
    amount = parse_int64(args[1])
    if amount is None:
        raise CommandError(NOT_AN_INTEGER)
    nx = xx = gt = lt = False
    for raw in args[2:]:
        option = raw.upper()
        if option == b"NX":
            nx = True
        elif option == b"XX":
            xx = True
        elif option == b"GT":
            gt = True
        elif option == b"LT":
            lt = True
        else:
            raise CommandError(f"ERR Unsupported option {safe_text(raw)}")
    if nx and (xx or gt or lt):
        raise CommandError("ERR NX and XX, GT or LT options at the same time are not compatible")
    if gt and lt:
        raise CommandError("ERR GT and LT options at the same time are not compatible")

    when = expiry_time(ctx, amount, unit_ms, absolute, name)
    store = ctx.store
    if not store.exists(key):
        return 0
    current = store.expire_at(key)
    if (nx and current is not None) or (xx and current is None):
        return 0
    if gt and (current is None or when <= current):  # no expiry counts as "infinitely far away"
        return 0
    if lt and current is not None and when >= current:
        return 0
    if when <= store.now():
        store.delete(key)  # a time in the past (or EXPIRE 0) deletes the key
        return 1
    store.set_expiry(key, when)
    return 1


def _register_expire_command(name: str, unit_ms: int, absolute: bool) -> None:
    @command(name, -3)
    def handler(ctx: Context, args: list[bytes]) -> object:
        return _expire_command(ctx, args, name, unit_ms, absolute)


_register_expire_command("expire", 1000, absolute=False)
_register_expire_command("pexpire", 1, absolute=False)
_register_expire_command("expireat", 1000, absolute=True)
_register_expire_command("pexpireat", 1, absolute=True)


@command("ttl", 2)
def ttl(ctx: Context, args: list[bytes]) -> object:
    """Seconds left (rounded to the nearest second); -2 no such key, -1 never expires."""
    remaining = ctx.store.ttl_ms(args[0])
    note_lookup(ctx, remaining != -2)
    return remaining if remaining < 0 else (remaining + 500) // 1000


@command("pttl", 2)
def pttl(ctx: Context, args: list[bytes]) -> object:
    remaining = ctx.store.ttl_ms(args[0])
    note_lookup(ctx, remaining != -2)
    return remaining


@command("persist", 2)
def persist(ctx: Context, args: list[bytes]) -> object:
    return 1 if ctx.store.persist(args[0]) else 0


# ---- the handshake: what clients send when they connect -------------------------------------

SERVER_NAME = b"miniredis"
SERVER_VERSION = b"0.1.0"
BAD_NAME = "ERR Client names cannot contain spaces, newlines or special characters."


def _is_plain_text(raw: bytes) -> bool:
    """Client names and library info may only hold printable ASCII without spaces."""
    return all(33 <= byte <= 126 for byte in raw)


def _client_id(ctx: Context, args: list[bytes]) -> object:
    return ctx.client.id


def _client_setname(ctx: Context, args: list[bytes]) -> object:
    if not _is_plain_text(args[0]):
        raise CommandError(BAD_NAME)
    ctx.client.name = args[0] or None  # an empty name clears it
    return OK


def _client_getname(ctx: Context, args: list[bytes]) -> object:
    return ctx.client.name  # None becomes a RESP null


def _client_setinfo(ctx: Context, args: list[bytes]) -> object:
    attribute, value = args
    kind = attribute.upper()
    if kind not in (b"LIB-NAME", b"LIB-VER"):
        raise CommandError(f"ERR Unrecognized option '{safe_text(attribute)}'")
    if not _is_plain_text(value):
        raise CommandError(
            f"ERR {safe_text(attribute)} cannot contain spaces, newlines or special characters."
        )
    if kind == b"LIB-NAME":
        ctx.client.lib_name = value or None
    else:
        ctx.client.lib_ver = value or None
    return OK


# subcommand -> (number of arguments after it, handler)
_CLIENT_SUBCOMMANDS: dict[bytes, tuple[int, Handler]] = {
    b"ID": (0, _client_id),
    b"SETNAME": (1, _client_setname),
    b"GETNAME": (0, _client_getname),
    b"SETINFO": (2, _client_setinfo),
}


@command("client", -2)
def client(ctx: Context, args: list[bytes]) -> object:
    spec = _CLIENT_SUBCOMMANDS.get(args[0].upper())
    if spec is None:
        raise CommandError(f"ERR unknown subcommand '{safe_text(args[0])}'. Try CLIENT HELP.")
    argument_count, handler = spec
    if len(args) - 1 != argument_count:
        raise wrong_arity(f"client|{args[0].decode('ascii').lower()}")
    return handler(ctx, args[1:])


@command("hello", -1)
def hello(ctx: Context, args: list[bytes]) -> object:
    """Pick a protocol version. MiniRedis only speaks RESP2, so only version 2 is accepted
    (real Redis also accepts 3); clients that ask for 3 get the same NOPROTO error that real Redis
    gives for versions it does not know, and can retry with 2."""
    name: bytes | None = None
    if args:
        version = parse_int64(args[0])
        if version is None:
            raise CommandError("ERR Protocol version is not an integer or out of range")
        if version != 2:
            raise CommandError("NOPROTO unsupported protocol version")
        position = 1
        while position < len(args):
            if args[position].upper() == b"SETNAME" and position + 1 < len(args):
                if not _is_plain_text(args[position + 1]):
                    raise CommandError(BAD_NAME)
                name = args[position + 1]
                position += 2
            else:
                option = safe_text(args[position])
                raise CommandError(f"ERR Syntax error in HELLO option '{option}'")
    if name is not None:  # only apply once every option has been checked
        ctx.client.name = name or None
    return [
        b"server", SERVER_NAME,
        b"version", SERVER_VERSION,
        b"proto", 2,
        b"id", ctx.client.id,
        b"mode", b"standalone",
        b"role", b"master",
        b"modules", [],
    ]  # fmt: skip


@command("command", -1)
def command_info(ctx: Context, args: list[bytes]) -> object:
    """Real Redis lists every command here. MiniRedis answers just enough for clients that ask."""
    if not args:
        return []
    subcommand = args[0].upper()
    if subcommand == b"COUNT":
        return len(COMMANDS)
    if subcommand == b"DOCS":
        return []
    raise CommandError(f"ERR unknown subcommand '{safe_text(args[0])}'. Try COMMAND HELP.")


@command("info", -1)
def info_(ctx: Context, args: list[bytes]) -> object:
    """A report on the server. Takes the time of one pass over all keys (for the memory figure)."""
    return render_info(ctx, [arg.decode("utf-8", "replace") for arg in args]).encode()


# ---- publish and subscribe --------------------------------------------------------------------


@command("subscribe", -2)
def subscribe(ctx: Context, args: list[bytes]) -> object:
    """Listen to channels. One confirmation per channel: [subscribe, channel, how many now]."""
    replies = Replies()
    for channel in args:
        count = ctx.pubsub.subscribe(ctx.client, channel)
        replies.append([b"subscribe", channel, count])
    return replies


@command("unsubscribe", -1)
def unsubscribe(ctx: Context, args: list[bytes]) -> object:
    """Stop listening: to the named channels, or to all of them when none are named."""
    channels = args or list(ctx.client.subscriptions)
    if not channels:
        return Replies([[b"unsubscribe", None, 0]])  # real Redis answers like this too
    replies = Replies()
    for channel in channels:
        count = ctx.pubsub.unsubscribe(ctx.client, channel)
        replies.append([b"unsubscribe", channel, count])
    return replies


@command("publish", 3)
def publish(ctx: Context, args: list[bytes]) -> object:
    """Send a message to a channel. The reply is how many clients received it."""
    return ctx.pubsub.publish(args[0], args[1])


@command("quit", -1)
def quit_(ctx: Context, args: list[bytes]) -> object:
    ctx.client.close_after_reply = True  # the server hangs up after sending this reply
    return OK

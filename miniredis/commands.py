"""Commands: the table that maps a command name to the code that runs it (steps 2.5 to 2.7).

`execute(ctx, words)` takes a parsed command such as [b"SET", b"a", b"1"] and returns a reply
value that miniredis.resp can encode.

RULE: a command handler must never `await`. The server is one thread running an event loop, so a
handler that never awaits cannot be interrupted, which is what makes every command atomic (INCR
cannot lose a count) without any locks. `test_commands_basic.py` checks that no handler is async.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from miniredis.resp import OK, PONG, RespError
from miniredis.store import Store

log = logging.getLogger("miniredis.commands")

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
# Redis is strict: no plus sign, no spaces, no leading zeros, and "-0" is not a number
_INTEGER = re.compile(rb"0|-?[1-9][0-9]{0,18}")
NOT_AN_INTEGER = "ERR value is not an integer or out of range"


@dataclass
class ClientState:
    """What the server remembers about one connected client."""

    id: int
    name: bytes | None = None
    lib_name: bytes | None = None
    lib_ver: bytes | None = None
    close_after_reply: bool = False


@dataclass
class Context:
    """Everything a command handler may use."""

    store: Store
    client: ClientState


class CommandError(Exception):
    """Raised by a handler to reply with an error. The text starts with its kind, e.g. 'ERR ...'."""


Handler = Callable[[Context, list[bytes]], object]


@dataclass(frozen=True)
class Command:
    name: str  # lowercase, as shown in error messages
    arity: int  # number of words including the name; negative means "at least"
    handler: Handler


COMMANDS: dict[bytes, Command] = {}


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
    try:
        return spec.handler(ctx, words[1:])
    except CommandError as exc:
        return RespError(str(exc))
    except Exception:  # a bug in one command must not take the whole server down
        log.exception("command %s crashed", spec.name)
        return RespError("ERR internal error")


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
    return args[0] if args else PONG


@command("echo", 2)
def echo(ctx: Context, args: list[bytes]) -> object:
    return args[0]


# ---- strings --------------------------------------------------------------------------------


@command("set", -3)
def set_(ctx: Context, args: list[bytes]) -> object:
    if len(args) > 2:
        # SET options (EX, NX, ...) arrive in step 3.1
        raise CommandError("ERR syntax error")
    ctx.store.set(args[0], args[1])
    return OK


@command("get", 2)
def get(ctx: Context, args: list[bytes]) -> object:
    return ctx.store.get(args[0])  # None becomes a RESP null


@command("del", -2)
def del_(ctx: Context, args: list[bytes]) -> object:
    """Delete keys; the reply is how many actually existed (DEL a a counts a only once)."""
    return sum(ctx.store.delete(key) for key in args)


@command("exists", -2)
def exists(ctx: Context, args: list[bytes]) -> object:
    """How many of the keys exist; a key listed twice is counted twice."""
    return sum(ctx.store.exists(key) for key in args)


def _increment(ctx: Context, key: bytes, amount: int) -> int:
    current = ctx.store.get(key)
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
    ctx.store.set(key, str(result).encode())
    return result


@command("incr", 2)
def incr(ctx: Context, args: list[bytes]) -> object:
    return _increment(ctx, args[0], 1)


@command("incrby", 3)
def incrby(ctx: Context, args: list[bytes]) -> object:
    # redis-py sends INCRBY key 1 even for a plain incr() (found in step 2.3)
    amount = parse_int64(args[1])
    if amount is None:
        raise CommandError(NOT_AN_INTEGER)
    return _increment(ctx, args[0], amount)


# ---- the handshake: what clients send when they connect (step 2.7) --------------------------

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


@command("quit", -1)
def quit_(ctx: Context, args: list[bytes]) -> object:
    ctx.client.close_after_reply = True  # the server hangs up after sending this reply
    return OK

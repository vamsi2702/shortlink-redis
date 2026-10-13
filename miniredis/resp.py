"""RESP (REdis Serialization Protocol): turn bytes into Python values and back (step 2.4).

Python value  <->  RESP bytes
    SimpleString("OK")   +OK\\r\\n          a short status
    RespError("ERR x")   -ERR x\\r\\n       an error (a reply, not a Python exception)
    int                  :42\\r\\n
    bytes / str          $5\\r\\nhello\\r\\n   a bulk string (binary safe)
    None                 $-1\\r\\n          null
    list / tuple         *2\\r\\n...        an array of any of the above

Parsing bytes -> values (what the server does with incoming commands):

    parser = Parser()
    parser.feed(data_from_the_socket)
    for command in parser.messages():     # a list of bytes, e.g. [b"SET", b"a", b"1"]
        ...

TCP is a stream, so `data` may hold half a command, or three commands. The parser keeps what it
cannot use yet and waits for the next `feed`.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

CRLF = b"\r\n"

# Limits so a hostile client cannot make the server use unbounded memory or CPU.
MAX_BULK_LENGTH = 512 * 1024 * 1024  # same default as real Redis
MAX_ARRAY_LENGTH = 1024 * 1024
MAX_LINE_LENGTH = 64 * 1024
MAX_DEPTH = 16  # arrays inside arrays

_INTEGER = re.compile(rb"-?[0-9]{1,19}")


class ProtocolError(Exception):
    """The bytes are not valid RESP. The server should send an error and close the connection."""


class SimpleString(str):
    """A RESP simple string (+OK). A subclass of str so it can be told apart from a bulk string."""


@dataclass(frozen=True)
class RespError:
    """A RESP error (-ERR ...). The message includes its kind, e.g. "ERR unknown command"."""

    message: str


OK = SimpleString("OK")
PONG = SimpleString("PONG")


# ---- encoding: Python value -> bytes --------------------------------------------------------


def encode(value: object) -> bytes:
    parts: list[bytes] = []
    _encode_into(value, parts)
    return b"".join(parts)


def encode_command(*words: object) -> bytes:
    """Encode a command the way a client sends it: an array of bulk strings."""
    return encode([w if isinstance(w, bytes | bytearray) else str(w).encode() for w in words])


def _single_line(text: str) -> bytes:
    data = text.encode()
    if b"\r" in data or b"\n" in data:
        # a newline here would let the text smuggle in extra replies
        raise ValueError("simple strings and errors must not contain CR or LF")
    return data


def _encode_into(value: object, out: list[bytes]) -> None:
    if value is None:
        out.append(b"$-1\r\n")
    elif isinstance(value, SimpleString):
        out.append(b"+" + _single_line(value) + CRLF)
    elif isinstance(value, RespError):
        out.append(b"-" + _single_line(value.message) + CRLF)
    elif isinstance(value, bool):
        raise TypeError("RESP2 has no booleans; use the integers 0 and 1")
    elif isinstance(value, int):
        out.append(b":%d\r\n" % value)
    elif isinstance(value, bytes | bytearray):
        out.append(b"$%d\r\n%b\r\n" % (len(value), value))
    elif isinstance(value, str):
        data = value.encode()  # the length counts BYTES, not characters
        out.append(b"$%d\r\n%b\r\n" % (len(data), data))
    elif isinstance(value, list | tuple):
        out.append(b"*%d\r\n" % len(value))
        for item in value:
            _encode_into(item, out)
    else:
        raise TypeError(f"cannot encode {type(value).__name__} as RESP")


# ---- parsing: bytes -> Python value ---------------------------------------------------------


def _read_line(buf: bytearray, pos: int) -> tuple[bytes, int] | None:
    """The text up to the next CRLF and the position after it, or None if no CRLF yet."""
    end = buf.find(CRLF, pos)
    if end == -1:
        if len(buf) - pos > MAX_LINE_LENGTH:
            raise ProtocolError("line too long")
        return None
    if end - pos > MAX_LINE_LENGTH:
        raise ProtocolError("line too long")
    return bytes(buf[pos:end]), end + 2


def _integer(text: bytes, what: str) -> int:
    if not _INTEGER.fullmatch(text):
        raise ProtocolError(f"invalid {what}: {text[:32]!r}")
    return int(text)


def _length(text: bytes, limit: int, what: str) -> int:
    """A bulk or array length: -1 (null) or 0..limit."""
    n = _integer(text, what)
    if n < -1 or n > limit:
        raise ProtocolError(f"invalid {what}: {n}")
    return n


def _parse(buf: bytearray, pos: int, depth: int = 0) -> tuple[object, int] | None:
    """Parse one value starting at `pos`.

    Returns (value, position after it), or None when more bytes are needed. The value itself
    may be None (a RESP null), which is why "need more bytes" is signalled by returning None
    instead of a tuple.
    """
    if pos >= len(buf):
        return None
    kind = buf[pos : pos + 1]
    line = _read_line(buf, pos + 1)
    if line is None:
        return None
    text, after = line

    if kind == b"+":
        return SimpleString(text.decode("utf-8", "replace")), after
    if kind == b"-":
        return RespError(text.decode("utf-8", "replace")), after
    if kind == b":":
        return _integer(text, "integer"), after
    if kind == b"$":
        n = _length(text, MAX_BULK_LENGTH, "bulk string length")
        if n == -1:
            return None, after
        end = after + n
        if len(buf) < end + 2:
            return None  # the promised bytes have not all arrived yet
        if buf[end : end + 2] != CRLF:
            raise ProtocolError("bulk string is not followed by CRLF")
        return bytes(buf[after:end]), end + 2
    if kind == b"*":
        n = _length(text, MAX_ARRAY_LENGTH, "array length")
        if n == -1:
            return None, after
        if depth >= MAX_DEPTH:
            raise ProtocolError("arrays nested too deeply")
        items: list[object] = []
        pos = after
        for _ in range(n):
            result = _parse(buf, pos, depth + 1)
            if result is None:
                return None
            item, pos = result
            items.append(item)
        return items, pos
    raise ProtocolError(f"unknown type byte {kind!r}")


class Parser:
    """Turns a stream of bytes into messages, however the bytes are split up.

    With `allow_inline=True`, plain-text lines such as `PING` or `SET a 1` are also accepted and
    become a list of words, like real Redis does for telnet users.
    """

    def __init__(self, *, allow_inline: bool = False) -> None:
        self._buf = bytearray()
        self._pos = 0  # start of the unread part of the buffer
        self._allow_inline = allow_inline
        # A top-level array is read one element at a time and remembered between calls, so a
        # huge command arriving in small pieces is parsed in linear time, not over and over.
        self._items: list[object] | None = None
        self._remaining = 0
        self._failed: ProtocolError | None = None

    @property
    def pending(self) -> bool:
        """True if some bytes have arrived that do not yet make a complete message."""
        return self._items is not None or len(self._buf) > self._pos

    def feed(self, data: bytes) -> None:
        self._buf += data

    def messages(self) -> Iterator[object]:
        """Yield every complete message currently buffered. Raises ProtocolError on bad input
        (after yielding the good messages that came before it). Use it up before feeding more."""
        if self._failed is not None:
            raise self._failed
        try:
            while True:
                try:
                    message = self._next_message()
                except ProtocolError as exc:
                    self._failed = exc  # the stream is out of sync; nothing more can be trusted
                    raise
                if message is None:
                    return
                yield message[0]
        finally:
            del self._buf[: self._pos]  # drop what was consumed, once, instead of per message
            self._pos = 0

    def _next_message(self) -> tuple[object] | None:
        """The next complete message as a 1-tuple (so a null message is distinguishable), or
        None if the buffer holds no complete message."""
        buf = self._buf
        while True:
            if self._items is not None:  # in the middle of a top-level array
                result = _parse(buf, self._pos, 1)
                if result is None:
                    return None
                item, self._pos = result
                self._items.append(item)
                self._remaining -= 1
                if self._remaining == 0:
                    items, self._items = self._items, None
                    return (items,)
                continue

            if self._pos >= len(buf):
                return None
            first = buf[self._pos]

            if first == ord("*"):
                line = _read_line(buf, self._pos + 1)
                if line is None:
                    return None
                text, self._pos = line
                n = _length(text, MAX_ARRAY_LENGTH, "array length")
                if n == -1:
                    return (None,)
                if n == 0:
                    return ([],)
                self._items, self._remaining = [], n
                continue

            if first in b"+-:$":
                result = _parse(buf, self._pos)
                if result is None:
                    return None
                value, self._pos = result
                return (value,)

            if not self._allow_inline:
                raise ProtocolError(f"unknown type byte {bytes([first])!r}")
            newline = buf.find(b"\n", self._pos)
            if newline == -1:
                if len(buf) - self._pos > MAX_LINE_LENGTH:
                    raise ProtocolError("line too long")
                return None
            if newline - self._pos > MAX_LINE_LENGTH:
                raise ProtocolError("line too long")
            words = bytes(buf[self._pos : newline]).split()
            self._pos = newline + 1
            if words:
                return (words,)
            # an empty line is ignored


def parse(data: bytes) -> object:
    """Parse exactly one complete RESP value. Handy in tests and tools."""
    parser = Parser()
    parser.feed(data)
    messages = list(parser.messages())
    if len(messages) != 1 or parser.pending:
        raise ValueError("expected exactly one complete RESP value")
    return messages[0]

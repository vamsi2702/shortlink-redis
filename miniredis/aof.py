"""The append-only file (AOF): a log of every change, so data survives a restart.

Each change that worked is appended to the file as a RESP command, in the order it happened.
Replaying the file from the top rebuilds the data. The commands logged are in a canonical form
that gives the same result whenever it is replayed: "SET k v EX 10" is logged as
"SET k v PXAT <the exact moment>", never as "10 seconds from now".

The log is written BEFORE the reply for the command is sent (see `commit`), so anything a client
has been told succeeded is in the file.
"""

import logging
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from miniredis.resp import Parser, ProtocolError, RespError, encode_command

log = logging.getLogger("miniredis.aof")

# When the operating system is told to really write the file to the disk (fsync):
#   always    after every batch of commands: safest and slowest
#   everysec  once a second, in the background: if the machine loses power you can lose about a
#             second of changes; a crash of MiniRedis itself loses nothing
#   no        never; the operating system decides
FSYNC_POLICIES = ("always", "everysec", "no")


@dataclass
class ReplayResult:
    """What happened when the file was replayed."""

    commands: int = 0  # commands replayed
    errors: int = 0  # commands that were replayed but answered with an error (and were skipped)
    valid_bytes: int = 0  # how much of the start of the file was good
    discarded_bytes: int = 0  # how much of the end was thrown away (and saved, see `replay`)
    reason: str | None = None  # why the end was thrown away, if it was


def _is_command(message: object) -> bool:
    return (
        isinstance(message, list)
        and len(message) > 0
        and all(isinstance(word, bytes) for word in message)
    )


def replay(
    path: str | Path, apply: Callable[[list[bytes]], object], chunk_size: int = 1 << 20
) -> ReplayResult:
    """Run every command in the file through `apply`, in order, to rebuild the data.

    A log can end badly: the machine lost power or the process was killed in the middle of a
    write, so the last command is only partly there, or the disk damaged some bytes. The commands
    before the damage are good, so they are used, and everything from the first bad byte on is
    cut off: it is copied to "<file>.discarded" first (nothing is lost silently), then the file is
    truncated there, so that new commands are appended after good data, not after garbage."""
    path = Path(path)
    result = ReplayResult()
    if not path.exists():
        return result
    size = path.stat().st_size
    parser = Parser()
    good_end = 0  # offset just past the last command that was replayed
    reason: str | None = None
    try:
        with open(path, "rb") as file:
            while chunk := file.read(chunk_size):
                parser.feed(chunk)
                for message in parser.messages():
                    if not _is_command(message):
                        reason = "something that is not a command"
                        break
                    reply = apply(message)  # type: ignore[arg-type]
                    result.commands += 1
                    if isinstance(reply, RespError):
                        result.errors += 1
                        if result.errors <= 10:
                            log.warning("replaying %r failed: %s", message[:3], reply.message)
                    good_end = parser.complete_offset
                if reason is not None:
                    break
    except ProtocolError as exc:
        reason = f"invalid data ({exc})"
    if reason is None and parser.pending:
        reason = "a command cut off at the end of the file"

    result.valid_bytes = good_end
    result.discarded_bytes = size - good_end
    if result.discarded_bytes > 0:
        result.reason = reason or "unknown"
        _cut_off_the_end(path, good_end)
        log.warning(
            "%s: discarded the last %d bytes after offset %d (%s); they were saved in %s",
            path,
            result.discarded_bytes,
            good_end,
            result.reason,
            _discarded_path(path),
        )
    return result


ITEMS_PER_COMMAND = 64  # a big list or hash is written as several commands of this many items
REWRITE_BUFFER = 1 << 20  # bytes collected before they are written to the new file


def rewrite_commands(
    items: Iterable[tuple[bytes, object, int | None]],
) -> Iterator[list[bytes]]:
    """The shortest list of commands that rebuilds `items` (key, value, dies-at or None), as
    produced by Store.items(): one command per key, or a few for a big list or hash."""
    for key, value, expire_at in items:
        if isinstance(value, bytes):
            if expire_at is None:
                yield [b"SET", key, value]
            else:
                yield [b"SET", key, value, b"PXAT", str(expire_at).encode()]
            continue
        if isinstance(value, deque):
            # LPUSH puts each value in front of the previous one, so to end up with the list
            # a b c (a first) the values must be pushed in the order c, b, a
            backwards = list(reversed(value))
            for start in range(0, len(backwards), ITEMS_PER_COMMAND):
                yield [b"LPUSH", key, *backwards[start : start + ITEMS_PER_COMMAND]]
        elif isinstance(value, dict):
            pairs = list(value.items())
            for start in range(0, len(pairs), ITEMS_PER_COMMAND):
                flat = [word for pair in pairs[start : start + ITEMS_PER_COMMAND] for word in pair]
                yield [b"HSET", key, *flat]
        else:
            raise TypeError(f"cannot write a {type(value).__name__} to the log")
        if expire_at is not None:
            yield [b"PEXPIREAT", key, str(expire_at).encode()]


def _fsync_directory(directory: Path) -> None:
    """Make a rename in this folder durable. Not possible (or needed) on Windows."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _discarded_path(path: Path) -> Path:
    return path.with_name(path.name + ".discarded")


def _cut_off_the_end(path: Path, good_end: int) -> None:
    """Save the bytes after `good_end` aside, then truncate the file there."""
    with open(path, "rb") as file:
        file.seek(good_end)
        tail = file.read()
    with open(_discarded_path(path), "ab") as saved:
        saved.write(tail)
        saved.flush()
        os.fsync(saved.fileno())  # the copy is safe before the original is cut
    with open(path, "r+b") as file:
        file.truncate(good_end)
        file.flush()
        os.fsync(file.fileno())


class AppendOnlyFile:
    def __init__(self, path: str | Path, fsync: str = "everysec") -> None:
        if fsync not in FSYNC_POLICIES:
            raise ValueError(f"fsync policy must be one of {FSYNC_POLICIES}, not {fsync!r}")
        self.path = Path(path)
        self.fsync_policy = fsync
        self.size = 0  # bytes in the file
        self.base_size = 0  # the size after the last rewrite (or when the server started)
        self.rewrites = 0  # how many times the file has been rewritten
        self.last_rewrite_seconds = 0.0  # how long the last rewrite took
        self.error: str | None = None  # the last write error, until a write succeeds again
        self._fd: int | None = None
        self._pending: list[bytes] = []  # logged by commands but not yet written to the file
        self._unsynced = False  # written to the file but not yet fsynced

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # a leftover from a rewrite that was interrupted: never part of the log, so remove it
        self.path.with_name(self.path.name + ".rewrite").unlink(missing_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        self._fd = os.open(self.path, flags, 0o644)
        self.size = os.fstat(self._fd).st_size
        self.base_size = self.size  # what is already there counts as the starting point

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    def append(self, words: list[bytes]) -> None:
        """Remember a change. It reaches the file at the next `commit`."""
        self._pending.append(encode_command(*words))

    def commit(self) -> None:
        """Write everything remembered to the file. The server calls this once per batch of
        commands, before sending the replies. Raises OSError if the write fails; the data not yet
        written is kept, so the next commit tries again."""
        if not self._pending:
            return
        assert self._fd is not None, "the file is not open"
        data = b"".join(self._pending)
        written = 0
        try:
            while written < len(data):
                written += os.write(self._fd, data[written:])  # may write less than asked
        except OSError as exc:
            self._pending = [data[written:]]
            self.size += written
            self.error = str(exc)
            raise
        self._pending.clear()
        self.size += written
        self.error = None
        self._unsynced = True
        if self.fsync_policy == "always":
            self.sync()

    def sync(self) -> None:
        """Ask the operating system to put the file on the disk for real."""
        if self._fd is None or not self._unsynced:
            return
        self._unsynced = False  # cleared first: a write during the fsync sets it again
        os.fsync(self._fd)

    def rewrite(self, items: Iterable[tuple[bytes, object, int | None]]) -> int:
        """Replace the file with the shortest log that rebuilds `items`. Returns the new size.

        The new log is written beside the old one and only moved into place, in one step, once it
        is complete and on the disk, so a crash or an error at any moment leaves a good log.
        Changes remembered by `append` but not yet written are already part of `items`, so they
        are dropped (writing them as well would apply them twice)."""
        assert self._fd is not None, "the file is not open"
        started = time.perf_counter()
        temporary = self.path.with_name(self.path.name + ".rewrite")
        try:
            with open(temporary, "wb") as out:
                chunk: list[bytes] = []
                chunk_size = 0
                for words in rewrite_commands(items):
                    data = encode_command(*words)
                    chunk.append(data)
                    chunk_size += len(data)
                    if chunk_size >= REWRITE_BUFFER:
                        out.write(b"".join(chunk))
                        chunk, chunk_size = [], 0
                out.write(b"".join(chunk))
                out.flush()
                os.fsync(out.fileno())  # the new log is on the disk before it replaces the old
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.close(self._fd)
        self._fd = None
        try:
            os.replace(temporary, self.path)  # the old log is replaced in one step
            self._pending.clear()
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            self.open()  # either the new file or, if the replace failed, the old one
        _fsync_directory(self.path.parent)
        self._unsynced = False
        self.base_size = self.size
        self.rewrites += 1
        self.last_rewrite_seconds = time.perf_counter() - started
        return self.size

    def close(self) -> None:
        if self._fd is None:
            return
        try:
            self.commit()
            self.sync()  # a clean shutdown puts everything on the disk, whatever the policy
        except OSError:
            log.exception("could not finish writing %s while closing", self.path)
        finally:
            os.close(self._fd)
            self._fd = None

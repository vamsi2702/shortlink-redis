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
from pathlib import Path

from miniredis.resp import encode_command

log = logging.getLogger("miniredis.aof")

# When the operating system is told to really write the file to the disk (fsync):
#   always    after every batch of commands: safest and slowest
#   everysec  once a second, in the background: if the machine loses power you can lose about a
#             second of changes; a crash of MiniRedis itself loses nothing
#   no        never; the operating system decides
FSYNC_POLICIES = ("always", "everysec", "no")


class AppendOnlyFile:
    def __init__(self, path: str | Path, fsync: str = "everysec") -> None:
        if fsync not in FSYNC_POLICIES:
            raise ValueError(f"fsync policy must be one of {FSYNC_POLICIES}, not {fsync!r}")
        self.path = Path(path)
        self.fsync_policy = fsync
        self.size = 0  # bytes in the file
        self.error: str | None = None  # the last write error, until a write succeeds again
        self._fd: int | None = None
        self._pending: list[bytes] = []  # logged by commands but not yet written to the file
        self._unsynced = False  # written to the file but not yet fsynced

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        self._fd = os.open(self.path, flags, 0o644)
        self.size = os.fstat(self._fd).st_size

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

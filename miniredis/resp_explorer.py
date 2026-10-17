"""Send one command to a Redis-compatible server and show the raw bytes in both directions.

A learning tool. Point it at real Redis (port 6379) now, at MiniRedis later.

    python -m miniredis.resp_explorer SET a 1
    python -m miniredis.resp_explorer --compact GET a
    python -m miniredis.resp_explorer --raw '*1\\r\\n$4\\r\\nPING\\r\\n'   # bytes you wrote by hand
"""

import argparse
import socket

from miniredis.resp import encode_command

REDIS_PORT = 6379


def command_to_resp(args: list[str]) -> bytes:
    """Encode a command the way clients send it: an array of bulk strings."""
    return encode_command(*args)


def unescape(text: str) -> bytes:
    r"""Turn the typed characters \r and \n into real CR and LF bytes (ASCII only)."""
    return text.encode("ascii").decode("unicode_escape").encode("latin-1")


def send_and_receive(host: str, port: int, payload: bytes, wait: float = 0.3) -> bytes:
    """Send bytes, then collect the reply until the server has been quiet for `wait` seconds."""
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(payload)
        sock.settimeout(wait)
        chunks: list[bytes] = []
        try:
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        except TimeoutError:
            pass  # quiet for `wait` seconds: the reply is complete
        return b"".join(chunks)


def describe(data: bytes) -> list[str]:
    """Explain RESP data line by line. Assumes string contents contain no CRLF (fine for a demo)."""
    pieces = data.split(b"\r\n")
    trailing = pieces.pop()  # whatever follows the last CRLF; empty if the data ended cleanly
    lines = [(piece, True) for piece in pieces]
    if trailing:
        lines.append((trailing, False))

    explained = []
    expecting_text = False
    for piece, complete in lines:
        shown = repr(piece + (b"\r\n" if complete else b""))
        text = piece.decode("utf-8", "replace")
        if expecting_text:
            meaning = "the string's bytes"
            expecting_text = False
        elif text.startswith("*"):
            meaning = "null array" if text == "*-1" else f"array of {text[1:]} items"
        elif text.startswith("$"):
            meaning = "null (no value)" if text == "$-1" else f"string of {text[1:]} bytes follows"
            expecting_text = text != "$-1"
        elif text.startswith("+"):
            meaning = "simple string"
        elif text.startswith("-"):
            meaning = "error"
        elif text.startswith(":"):
            meaning = "integer"
        else:
            meaning = "?"
        if not complete:
            meaning += " (incomplete: no CRLF yet)"
        explained.append(f"  {shown:<40} {meaning}")
    return explained


def main() -> None:
    parser = argparse.ArgumentParser(description="Show the raw RESP bytes of a command and reply")
    parser.add_argument("command", nargs="*", help="command words, e.g. SET a 1")
    parser.add_argument("--raw", help=r"send these exact bytes instead; write CRLF as \r\n")
    parser.add_argument("--compact", action="store_true", help="only print the two byte strings")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=REDIS_PORT)
    args = parser.parse_args()

    if args.raw is not None:
        payload = unescape(args.raw)
    elif args.command:
        payload = command_to_resp(args.command)
    else:
        parser.error("give a command (SET a 1) or --raw")

    reply = send_and_receive(args.host, args.port, payload)

    if args.compact:
        print(f"sent:     {payload!r}")
        print(f"received: {reply!r}")
        return
    print(f"sent {len(payload)} bytes:")
    print("\n".join(describe(payload)))
    print(f"received {len(reply)} bytes:")
    print("\n".join(describe(reply)) if reply else "  (nothing)")


if __name__ == "__main__":
    main()

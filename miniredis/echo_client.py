"""A tiny client for the echo server: type a line, see it come back."""

import argparse
import socket

from miniredis.echo_server import DEFAULT_HOST, DEFAULT_PORT


def recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes. TCP is a stream, so one recv() may return only part of a reply."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("server closed the connection before sending everything")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def echo(sock: socket.socket, text: str) -> str:
    data = text.encode()
    sock.sendall(data)
    return recv_exactly(sock, len(data)).decode()


def main() -> None:
    parser = argparse.ArgumentParser(description="Echo client")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("messages", nargs="*", help="messages to send; omit to type interactively")
    args = parser.parse_args()

    with socket.create_connection((args.host, args.port)) as sock:
        if args.messages:
            for message in args.messages:
                print(f"sent: {message!r}  received: {echo(sock, message)!r}")
            return
        print("Type a line and press Enter (Ctrl+C to quit)")
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                break
            if line:
                print(f"received: {echo(sock, line)!r}")


if __name__ == "__main__":
    main()

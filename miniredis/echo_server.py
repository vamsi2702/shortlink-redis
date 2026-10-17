"""A plain blocking TCP echo server: the simplest possible network server.

It sends back whatever bytes it receives. It serves ONE client at a time; async_echo_server.py
fixes that.
"""

import argparse
import socket
import sys
import threading

DEFAULT_HOST = "127.0.0.1"  # this machine only; use 0.0.0.0 to accept outside connections
DEFAULT_PORT = 6380  # next to Redis's 6379, so both can run side by side
RECV_SIZE = 4096


def create_listener(host: str, port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        # lets us restart right away on Linux/Mac; on Windows this flag means something
        # different and unsafe (two programs sharing a port), so we skip it there
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen()
    return listener


def handle_client(conn: socket.socket) -> None:
    """Echo everything one client sends until it hangs up."""
    with conn:
        while True:
            data = conn.recv(RECV_SIZE)  # waits (blocks) until bytes arrive
            if not data:  # empty bytes means the client closed the connection
                break
            conn.sendall(data)


def serve_forever(
    listener: socket.socket, stop: threading.Event | None = None, poll_interval: float = 0.5
) -> None:
    """Accept clients one after another. Stops when `stop` is set or the listener is closed."""
    stop = stop or threading.Event()
    # a timeout on accept() lets the loop notice `stop` (and Ctrl+C on Windows) every so often
    listener.settimeout(poll_interval)
    while not stop.is_set():
        try:
            conn, addr = listener.accept()
        except TimeoutError:
            continue
        except OSError:  # the listener was closed
            break
        conn.settimeout(None)  # make sure reads on this client really block
        print(f"client connected: {addr[0]}:{addr[1]}", flush=True)
        try:
            handle_client(conn)
        except OSError as exc:  # e.g. the client crashed and the connection was reset
            print(f"client error: {exc}", flush=True)
        print(f"client disconnected: {addr[0]}:{addr[1]}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Blocking TCP echo server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    listener = create_listener(args.host, args.port)
    print(f"echo server listening on {args.host}:{args.port} (Ctrl+C to stop)", flush=True)
    try:
        serve_forever(listener)
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        listener.close()


if __name__ == "__main__":
    main()

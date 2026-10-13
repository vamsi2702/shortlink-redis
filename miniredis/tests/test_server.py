import threading

import pytest

from miniredis import commands
from miniredis.resp import OK, PONG, RespError, encode_command


def test_ping_over_a_real_socket(client):
    assert client.call("PING") == PONG


def test_set_and_get_over_a_real_socket(client):
    assert client.call("SET", "a", "1") == OK
    assert client.call("GET", "a") == b"1"
    assert client.call("GET", "missing") is None


def test_unknown_command_and_wrong_arity_get_error_replies(client):
    assert client.call("FOO", "x") == RespError(
        "ERR unknown command 'FOO', with args beginning with: 'x' "
    )
    assert client.call("GET") == RespError("ERR wrong number of arguments for 'get' command")


def test_the_connection_stays_usable_after_an_error_reply(client):
    client.call("NOSUCHTHING")

    assert client.call("PING") == PONG


def test_several_clients_share_one_store(connect):
    first, second = connect(), connect()

    first.call("SET", "shared", "from first")

    assert second.call("GET", "shared") == b"from first"


def test_pipelined_commands_are_answered_in_order(client):
    client.send_raw(
        encode_command("SET", "k", "v") + encode_command("GET", "k") + encode_command("PING")
    )

    assert [client.read(), client.read(), client.read()] == [OK, b"v", PONG]


def test_a_command_arriving_in_tiny_pieces_is_still_understood(client):
    raw = encode_command("SET", "slow", "value")

    for i in range(len(raw)):
        client.send_raw(raw[i : i + 1])

    assert client.read() == OK
    assert client.call("GET", "slow") == b"value"


def test_a_command_split_in_the_middle_of_a_value(client):
    raw = encode_command("SET", "k", "x" * 1000)
    client.send_raw(raw[:500])
    with pytest.raises(TimeoutError):
        client.read(timeout=0.2)  # nothing yet: the server is waiting for the rest

    client.send_raw(raw[500:])

    assert client.read(timeout=5) == OK


def test_plain_text_inline_commands_work_like_in_telnet(client):
    client.send_raw(b"PING\r\n")
    assert client.read() == PONG

    client.send_raw(b"SET name vamsi\r\n")
    assert client.read() == OK

    client.send_raw(b"GET name\r\n")
    assert client.read() == b"vamsi"


def test_an_empty_command_gets_no_reply(client):
    client.send_raw(b"*0\r\n" + encode_command("PING"))

    assert client.read() == PONG  # the only reply is for the PING


def test_a_one_megabyte_value_round_trips(client):
    value = bytes(range(256)) * 4096

    assert client.call("SET", b"big", value) == OK
    assert client.call("GET", b"big") == value


def test_values_with_crlf_inside_do_not_confuse_the_protocol(client):
    value = b"line1\r\nline2\r\n+OK\r\n"

    client.call("SET", "tricky", value)

    assert client.call("GET", "tricky") == value


def test_fifty_clients_at_once(connect):
    clients = [connect() for _ in range(50)]

    for i, c in enumerate(clients):
        c.send_raw(encode_command("SET", f"key{i}", f"value{i}"))
    assert [c.read() for c in clients] == [OK] * 50

    for i, c in enumerate(clients):
        c.send_raw(encode_command("GET", f"key{i}"))
    assert [c.read() for c in clients] == [f"value{i}".encode() for i in range(50)]


def test_a_slow_client_does_not_block_the_others(connect):
    stuck = connect()
    stuck.send_raw(b"*2\r\n$3\r\nGET\r\n$100\r\nonly a few bytes")  # waiting for 90 more bytes
    other = connect()

    assert other.call("PING") == PONG  # served immediately


# ---- bad input ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [b"*abc\r\n", b"*1\r\n:5\r\n", b"*1\r\n*1\r\n$1\r\na\r\n", b"*1\r\n$3\r\nabcXY", b"*-5\r\n"],
)
def test_invalid_resp_gets_a_protocol_error_and_the_connection_is_closed(connect, garbage):
    bad = connect()
    bad.send_raw(garbage)

    reply = bad.read()

    assert isinstance(reply, RespError)
    assert reply.message.startswith("ERR Protocol error")
    assert bad.server_closed_connection()


def test_commands_before_the_bad_bytes_are_still_answered(client):
    client.send_raw(encode_command("SET", "k", "v") + b"*abc\r\n")

    assert client.read() == OK
    assert isinstance(client.read(), RespError)
    assert client.server_closed_connection()


def test_one_clients_bad_bytes_do_not_affect_other_clients(connect):
    good, bad = connect(), connect()
    good.call("SET", "k", "v")

    bad.send_raw(b"*abc\r\n")
    assert isinstance(bad.read(), RespError)

    assert good.call("GET", "k") == b"v"


def test_a_client_that_vanishes_mid_command_does_not_hurt_the_server(connect):
    quitter = connect()
    quitter.send_raw(b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$10\r\nhalf")
    quitter.close()

    assert connect().call("PING") == PONG


def test_a_crashing_command_does_not_close_the_connection(client, monkeypatch):
    def broken(ctx, args):
        raise RuntimeError("bug")

    monkeypatch.setitem(commands.COMMANDS, b"BOOM", commands.Command("boom", 1, broken))

    assert client.call("BOOM") == RespError("ERR internal error")
    assert client.call("PING") == PONG


def test_a_handler_returning_something_unencodable_gives_an_error_reply(client, monkeypatch):
    monkeypatch.setitem(
        commands.COMMANDS, b"WEIRD", commands.Command("weird", 1, lambda ctx, args: object())
    )

    assert client.call("WEIRD") == RespError("ERR internal error")
    assert client.call("PING") == PONG


def test_the_server_handles_commands_from_many_threads_at_once(server, connect):
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            c = connect()
            for i in range(50):
                assert c.call("SET", f"t{n}:{i}", str(i)) == OK
                assert c.call("GET", f"t{n}:{i}") == str(i).encode()
        except Exception as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == []
    assert len(server.store) == 500

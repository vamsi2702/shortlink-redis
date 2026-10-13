import inspect
import logging

import pytest

from miniredis import commands
from miniredis.commands import COMMANDS, ClientState, Context, execute
from miniredis.resp import OK, PONG, RespError
from miniredis.store import Store


@pytest.fixture
def ctx():
    return Context(store=Store(), client=ClientState(id=1))


def run(ctx, *words):
    return execute(ctx, [w if isinstance(w, bytes) else str(w).encode() for w in words])


def error_text(reply) -> str:
    assert isinstance(reply, RespError), reply
    return reply.message


# ---- the rule that makes commands atomic ----------------------------------------------------


def test_no_command_handler_is_async():
    """A handler that awaits could be interrupted halfway, and the server relies on that never."""
    for spec in COMMANDS.values():
        assert not inspect.iscoroutinefunction(spec.handler), spec.name


# ---- PING and ECHO --------------------------------------------------------------------------


def test_ping(ctx):
    assert run(ctx, "PING") == PONG


def test_ping_with_a_message_echoes_it_as_a_bulk_string(ctx):
    assert run(ctx, "PING", "hello") == b"hello"
    assert run(ctx, "PING", "") == b""


def test_ping_with_too_many_arguments(ctx):
    reply = run(ctx, "PING", "a", "b")

    assert error_text(reply) == "ERR wrong number of arguments for 'ping' command"


def test_echo(ctx):
    assert run(ctx, "ECHO", "hi") == b"hi"
    assert run(ctx, "ECHO", "") == b""


# ---- SET and GET ----------------------------------------------------------------------------


def test_set_then_get(ctx):
    assert run(ctx, "SET", "a", "1") == OK
    assert run(ctx, "GET", "a") == b"1"


def test_get_of_a_missing_key_is_null(ctx):
    assert run(ctx, "GET", "nothing") is None


def test_set_overwrites(ctx):
    run(ctx, "SET", "a", "1")
    run(ctx, "SET", "a", "2")

    assert run(ctx, "GET", "a") == b"2"


def test_keys_and_values_are_binary_safe(ctx):
    key = b"key with spaces\r\nand \x00 \xff bytes"
    value = bytes(range(256))

    assert run(ctx, b"SET", key, value) == OK
    assert run(ctx, b"GET", key) == value


def test_empty_key_and_empty_value_are_allowed(ctx):
    run(ctx, "SET", "", "")

    assert run(ctx, "GET", "") == b""


def test_keys_are_case_sensitive(ctx):
    run(ctx, "SET", "Key", "1")

    assert run(ctx, "GET", "key") is None


def test_set_with_options_is_not_supported_yet(ctx):
    assert error_text(run(ctx, "SET", "a", "1", "EX", "10")) == "ERR syntax error"
    assert run(ctx, "GET", "a") is None  # and nothing was stored


# ---- command names and arity ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["get", "GET", "GeT", "gEt"])
def test_command_names_are_case_insensitive(ctx, name):
    run(ctx, "SET", "a", "1")

    assert run(ctx, name, "a") == b"1"


@pytest.mark.parametrize(
    ("words", "command"),
    [
        (["GET"], "get"),
        (["GeT", "a", "b"], "get"),
        (["SET"], "set"),
        (["SET", "a"], "set"),
        (["ECHO"], "echo"),
        (["ECHO", "a", "b"], "echo"),
    ],
)
def test_wrong_number_of_arguments_names_the_command_in_lowercase(ctx, words, command):
    assert error_text(run(ctx, *words)) == f"ERR wrong number of arguments for '{command}' command"


def test_unknown_command_echoes_the_name_as_typed_and_the_arguments(ctx):
    assert error_text(run(ctx, "FoO")) == "ERR unknown command 'FoO', with args beginning with: "
    assert (
        error_text(run(ctx, "foo", "a", "b"))
        == "ERR unknown command 'foo', with args beginning with: 'a' 'b' "
    )


def test_unknown_command_error_cannot_be_used_to_inject_extra_replies(ctx):
    reply = run(ctx, b"evil\r\n+OK", b"x\r\ny")

    assert "\r" not in reply.message
    assert "\n" not in reply.message


def test_unknown_command_echoes_at_most_128_bytes_of_arguments_cutting_the_last_one(ctx):
    reply = run(ctx, "foo", *["x" * 60] * 10)

    # 60 + 60 bytes fit, then only 2 more bytes of the third argument, exactly like real Redis
    assert reply.message == (
        "ERR unknown command 'foo', with args beginning with: "
        f"'{'x' * 60}' '{'x' * 60}' 'xx' "
    )


def test_unknown_command_name_is_cut_at_128_bytes(ctx):
    reply = run(ctx, "n" * 500)

    assert reply.message.startswith(f"ERR unknown command '{'n' * 128}', with")


# ---- robustness -----------------------------------------------------------------------------


def test_a_crashing_handler_becomes_an_error_reply(ctx, monkeypatch, caplog):
    def broken(ctx, args):
        raise RuntimeError("bug in the handler")

    monkeypatch.setitem(
        COMMANDS, b"BOOM", commands.Command(name="boom", arity=1, handler=broken)
    )

    with caplog.at_level(logging.ERROR, logger="miniredis.commands"):
        reply = run(ctx, "BOOM")

    assert error_text(reply) == "ERR internal error"
    assert "bug in the handler" in caplog.text
    assert run(ctx, "PING") == PONG

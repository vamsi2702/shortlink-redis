import pytest

from miniredis.commands import COMMANDS, ClientState, Context, execute
from miniredis.resp import OK, RespError
from miniredis.store import Store


@pytest.fixture
def ctx():
    return Context(store=Store(), client=ClientState(id=7))


def run(ctx, *words):
    return execute(ctx, [w if isinstance(w, bytes) else str(w).encode() for w in words])


# ---- CLIENT ---------------------------------------------------------------------------------


def test_client_id_is_this_connections_number(ctx):
    assert run(ctx, "CLIENT", "ID") == 7


def test_client_setname_and_getname(ctx):
    assert run(ctx, "CLIENT", "GETNAME") is None
    assert run(ctx, "CLIENT", "SETNAME", "mini") == OK
    assert run(ctx, "CLIENT", "GETNAME") == b"mini"


def test_an_empty_name_clears_the_name(ctx):
    run(ctx, "CLIENT", "SETNAME", "mini")

    assert run(ctx, "CLIENT", "SETNAME", "") == OK
    assert run(ctx, "CLIENT", "GETNAME") is None


@pytest.mark.parametrize("bad", ["has space", "tab\there", "new\nline", "del\x7f", "é", "a\x00b"])
def test_client_names_may_only_be_printable_ascii_without_spaces(ctx, bad):
    reply = run(ctx, "CLIENT", "SETNAME", bad.encode())

    assert reply == RespError(
        "ERR Client names cannot contain spaces, newlines or special characters."
    )
    assert run(ctx, "CLIENT", "GETNAME") is None


def test_client_setinfo_is_accepted_and_remembered(ctx):
    assert run(ctx, "CLIENT", "SETINFO", "LIB-NAME", "redis-py") == OK
    assert run(ctx, "CLIENT", "SETINFO", "lib-ver", "8.1.0") == OK
    assert ctx.client.lib_name == b"redis-py"
    assert ctx.client.lib_ver == b"8.1.0"


def test_client_setinfo_rejects_unknown_options_and_bad_values(ctx):
    assert run(ctx, "CLIENT", "SETINFO", "foo", "bar") == RespError("ERR Unrecognized option 'foo'")
    assert run(ctx, "CLIENT", "SETINFO", "LIB-NAME", "a b") == RespError(
        "ERR LIB-NAME cannot contain spaces, newlines or special characters."
    )


@pytest.mark.parametrize(
    ("words", "label"),
    [
        (["CLIENT"], "client"),
        (["CLIENT", "SETNAME"], "client|setname"),
        (["CLIENT", "SETNAME", "a", "b"], "client|setname"),
        (["CLIENT", "GETNAME", "x"], "client|getname"),
        (["CLIENT", "ID", "x"], "client|id"),
        (["CLIENT", "SETINFO", "LIB-NAME"], "client|setinfo"),
    ],
)
def test_client_argument_count_errors(ctx, words, label):
    assert run(ctx, *words) == RespError(f"ERR wrong number of arguments for '{label}' command")


def test_unknown_client_subcommand_is_an_error_not_a_crash(ctx):
    # redis-py 8 sends this on connect and carries on happily when it gets an error back
    reply = run(ctx, "CLIENT", "MAINT_NOTIFICATIONS", "ON")

    assert reply == RespError("ERR unknown subcommand 'MAINT_NOTIFICATIONS'. Try CLIENT HELP.")


def test_client_subcommands_are_case_insensitive(ctx):
    assert run(ctx, "client", "SetName", "x") == OK
    assert run(ctx, "CLIENT", "getname") == b"x"


# ---- HELLO ----------------------------------------------------------------------------------


def test_hello_2_describes_the_server_like_real_redis_does(ctx):
    reply = run(ctx, "HELLO", "2")

    assert len(reply) == 14
    fields = dict(zip(reply[::2], reply[1::2], strict=True))
    assert fields[b"server"] == b"miniredis"
    assert fields[b"proto"] == 2
    assert fields[b"id"] == 7
    assert fields[b"mode"] == b"standalone"
    assert fields[b"role"] == b"master"
    assert fields[b"modules"] == []


def test_hello_without_arguments_also_works(ctx):
    assert len(run(ctx, "HELLO")) == 14


@pytest.mark.parametrize("version", ["3", "1", "0", "4", "-1"])
def test_hello_refuses_other_protocol_versions_with_noproto(ctx, version):
    assert run(ctx, "HELLO", version) == RespError("NOPROTO unsupported protocol version")


@pytest.mark.parametrize("version", ["x", "2.0", "", "+2"])
def test_hello_with_a_version_that_is_not_an_integer(ctx, version):
    assert run(ctx, "HELLO", version) == RespError(
        "ERR Protocol version is not an integer or out of range"
    )


def test_hello_setname(ctx):
    assert len(run(ctx, "HELLO", "2", "SETNAME", "mini")) == 14
    assert run(ctx, "CLIENT", "GETNAME") == b"mini"


def test_hello_with_an_unknown_option(ctx):
    assert run(ctx, "HELLO", "2", "foo") == RespError("ERR Syntax error in HELLO option 'foo'")


def test_hello_changes_nothing_if_any_option_is_bad(ctx):
    run(ctx, "HELLO", "2", "SETNAME", "mini", "foo")

    assert run(ctx, "CLIENT", "GETNAME") is None


def test_hello_setname_without_a_name_is_a_syntax_error(ctx):
    assert run(ctx, "HELLO", "2", "SETNAME") == RespError(
        "ERR Syntax error in HELLO option 'SETNAME'"
    )


def test_hello_setname_with_a_bad_name(ctx):
    reply = run(ctx, "HELLO", "2", "SETNAME", "has space")

    assert reply == RespError(
        "ERR Client names cannot contain spaces, newlines or special characters."
    )


# ---- COMMAND and QUIT -----------------------------------------------------------------------


def test_command_count_is_the_number_of_commands_we_have(ctx):
    assert run(ctx, "COMMAND", "COUNT") == len(COMMANDS)
    assert len(COMMANDS) >= 12


def test_command_with_no_arguments_and_command_docs_are_accepted(ctx):
    assert run(ctx, "COMMAND") == []
    assert run(ctx, "COMMAND", "DOCS") == []


def test_unknown_command_subcommand(ctx):
    assert run(ctx, "COMMAND", "bogus") == RespError(
        "ERR unknown subcommand 'bogus'. Try COMMAND HELP."
    )


def test_quit_replies_ok_and_asks_the_server_to_hang_up(ctx):
    assert ctx.client.close_after_reply is False

    assert run(ctx, "QUIT") == OK
    assert ctx.client.close_after_reply is True

import random

import pytest

from miniredis.resp import (
    MAX_ARRAY_LENGTH,
    MAX_BULK_LENGTH,
    MAX_DEPTH,
    MAX_LINE_LENGTH,
    OK,
    PONG,
    Parser,
    ProtocolError,
    RespError,
    SimpleString,
    encode,
    encode_command,
    parse,
)

# What real Redis 7.4 sent and received, recorded in step 2.3 (see docs/RESP.md).
HELLO_2_REPLY = (
    b"*14\r\n$6\r\nserver\r\n$5\r\nredis\r\n$7\r\nversion\r\n$6\r\n7.4.11\r\n$5\r\nproto\r\n:2\r\n"
    b"$2\r\nid\r\n:105\r\n$4\r\nmode\r\n$10\r\nstandalone\r\n$4\r\nrole\r\n$6\r\nmaster\r\n"
    b"$7\r\nmodules\r\n*0\r\n"
)
HELLO_2_VALUE = [
    b"server", b"redis", b"version", b"7.4.11", b"proto", 2, b"id", 105,
    b"mode", b"standalone", b"role", b"master", b"modules", [],
]  # fmt: skip

REAL_REDIS = [
    (b"+OK\r\n", SimpleString("OK")),
    (b"+PONG\r\n", SimpleString("PONG")),
    (b"$5\r\nhello\r\n", b"hello"),
    (b"$1\r\n1\r\n", b"1"),
    (b"$-1\r\n", None),
    (b":2\r\n", 2),
    (b":-2\r\n", -2),
    (b":-1\r\n", -1),
    (b"-ERR value is not an integer or out of range\r\n",
     RespError("ERR value is not an integer or out of range")),
    (b"-WRONGTYPE Operation against a key holding the wrong kind of value\r\n",
     RespError("WRONGTYPE Operation against a key holding the wrong kind of value")),
    (b"-ERR unknown command 'FOO', with args beginning with: \r\n",
     RespError("ERR unknown command 'FOO', with args beginning with: ")),
    (HELLO_2_REPLY, HELLO_2_VALUE),
    # what redis-py sent in step 2.3
    (b"*4\r\n$6\r\nCLIENT\r\n$7\r\nSETINFO\r\n$8\r\nLIB-NAME\r\n$8\r\nredis-py\r\n",
     [b"CLIENT", b"SETINFO", b"LIB-NAME", b"redis-py"]),
    (b"*5\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\nv\r\n$2\r\nEX\r\n$3\r\n100\r\n",
     [b"SET", b"k", b"v", b"EX", b"100"]),
    (b"*3\r\n$6\r\nINCRBY\r\n$1\r\nc\r\n$1\r\n5\r\n", [b"INCRBY", b"c", b"5"]),
]  # fmt: skip


def messages_from(*chunks: bytes, **options) -> list:
    """Feed the chunks one after another and collect every message the parser yields."""
    parser = Parser(**options)
    found: list = []
    for chunk in chunks:
        parser.feed(chunk)
        found.extend(parser.messages())
    return found


# ---- encoding -------------------------------------------------------------------------------


def test_set_a_1_is_the_27_bytes_worked_out_by_hand():
    assert encode_command("SET", "a", "1") == b"*3\r\n$3\r\nSET\r\n$1\r\na\r\n$1\r\n1\r\n"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (OK, b"+OK\r\n"),
        (PONG, b"+PONG\r\n"),
        (RespError("ERR boom"), b"-ERR boom\r\n"),
        (42, b":42\r\n"),
        (-7, b":-7\r\n"),
        (0, b":0\r\n"),
        (b"hello", b"$5\r\nhello\r\n"),
        (bytearray(b"hi"), b"$2\r\nhi\r\n"),
        ("hello", b"$5\r\nhello\r\n"),
        ("", b"$0\r\n\r\n"),
        (b"", b"$0\r\n\r\n"),
        (None, b"$-1\r\n"),
        ([], b"*0\r\n"),
        ([b"a", 1], b"*2\r\n$1\r\na\r\n:1\r\n"),
        ((b"a", b"b"), b"*2\r\n$1\r\na\r\n$1\r\nb\r\n"),
        ([[b"x"], []], b"*2\r\n*1\r\n$1\r\nx\r\n*0\r\n"),
        ([None, OK], b"*2\r\n$-1\r\n+OK\r\n"),
    ],
)
def test_encode(value, expected):
    assert encode(value) == expected


def test_bulk_string_length_counts_bytes_not_characters():
    assert encode("é") == b"$2\r\n\xc3\xa9\r\n"
    assert encode("✓") == b"$3\r\n\xe2\x9c\x93\r\n"


def test_bulk_string_may_contain_crlf_because_of_the_length_prefix():
    assert encode(b"a\r\nb") == b"$4\r\na\r\nb\r\n"


def test_encode_command_turns_every_word_into_a_bulk_string():
    assert encode_command("INCRBY", "c", 5) == b"*3\r\n$6\r\nINCRBY\r\n$1\r\nc\r\n$1\r\n5\r\n"
    assert encode_command(b"GET", SimpleString("k")) == b"*2\r\n$3\r\nGET\r\n$1\r\nk\r\n"


def test_simple_string_and_error_text_cannot_smuggle_in_extra_replies():
    with pytest.raises(ValueError):
        encode(SimpleString("OK\r\n:1000"))
    with pytest.raises(ValueError):
        encode(RespError("ERR x\nnext"))


def test_encode_refuses_types_resp2_cannot_represent():
    for bad in (True, 1.5, {"a": 1}, {1}, object()):
        with pytest.raises(TypeError):
            encode(bad)
    with pytest.raises(TypeError):
        encode([b"fine", 1.5])


# ---- parsing a complete message -------------------------------------------------------------


@pytest.mark.parametrize(("raw", "value"), REAL_REDIS)
def test_parse_what_real_redis_sends(raw, value):
    assert parse(raw) == value


@pytest.mark.parametrize(("raw", "value"), REAL_REDIS)
def test_encoding_the_parsed_value_gives_back_the_same_bytes(raw, value):
    assert encode(parse(raw)) == raw


def test_parse_keeps_simple_strings_and_bulk_strings_apart():
    assert isinstance(parse(b"+OK\r\n"), SimpleString)
    assert not isinstance(parse(b"$2\r\nOK\r\n"), SimpleString)
    assert parse(b"$2\r\nOK\r\n") == b"OK"


def test_parse_null_array_empty_array_and_empty_string():
    assert parse(b"*-1\r\n") is None
    assert parse(b"*0\r\n") == []
    assert parse(b"$0\r\n\r\n") == b""


def test_parse_bulk_string_containing_crlf_and_binary_bytes():
    payload = b"line1\r\nline2\x00\xff"

    assert parse(b"$%d\r\n%b\r\n" % (len(payload), payload)) == payload


def test_parse_nested_arrays_with_mixed_types():
    raw = b"*3\r\n*2\r\n:1\r\n$1\r\na\r\n$-1\r\n*0\r\n"

    assert parse(raw) == [[1, b"a"], None, []]


def test_parse_integers():
    assert parse(b":0\r\n") == 0
    assert parse(b":-5\r\n") == -5
    assert parse(b":1234567890123\r\n") == 1234567890123


def test_parse_refuses_trailing_bytes_or_an_incomplete_message():
    with pytest.raises(ValueError):
        parse(b"+OK\r\n+OK\r\n")
    with pytest.raises(ValueError):
        parse(b"$5\r\nhel")
    with pytest.raises(ValueError):
        parse(b"")


# ---- the stream: partial data and several messages ------------------------------------------


def test_several_commands_in_one_read_come_out_in_order():
    stream = encode_command("SET", "a", "1") + encode_command("GET", "a") + b"+OK\r\n"

    assert messages_from(stream) == [[b"SET", b"a", b"1"], [b"GET", b"a"], SimpleString("OK")]


def test_nothing_comes_out_until_the_last_byte_arrives():
    raw = encode_command("SET", "greeting", "hello world")
    parser = Parser()

    for byte in raw[:-1]:
        parser.feed(bytes([byte]))
        assert list(parser.messages()) == []
        assert parser.pending
    parser.feed(raw[-1:])

    assert list(parser.messages()) == [[b"SET", b"greeting", b"hello world"]]
    assert not parser.pending


def test_the_promised_but_unsent_bytes_case_from_the_exercises():
    parser = Parser()
    parser.feed(b"*2\r\n$3\r\nGET\r\n$10\r\nshort")
    assert list(parser.messages()) == []  # still waiting for 5 more bytes

    parser.feed(b"-ones")  # 10 bytes now, but the closing CRLF is still missing
    assert list(parser.messages()) == []

    parser.feed(b"\r\n")

    assert list(parser.messages()) == [[b"GET", b"short-ones"]]


def test_the_start_of_the_next_message_is_kept_for_later():
    parser = Parser()
    parser.feed(b"+OK\r\n:12")
    assert list(parser.messages()) == [SimpleString("OK")]

    parser.feed(b"3\r\n")

    assert list(parser.messages()) == [123]


STREAM = (
    encode_command("SET", "a", "1")
    + b"+PONG\r\n"
    + HELLO_2_REPLY
    + b"$-1\r\n"
    + encode(["nested", [1, [None, b"x"]], b"a\r\nb"])
    + b"-ERR nope\r\n"
    + b"*0\r\n"
)
STREAM_MESSAGES = messages_from(STREAM)


def test_the_sample_stream_parses_as_expected():
    assert len(STREAM_MESSAGES) == 7
    assert STREAM_MESSAGES[0] == [b"SET", b"a", b"1"]
    assert STREAM_MESSAGES[1] == PONG
    assert STREAM_MESSAGES[2] == HELLO_2_VALUE
    assert STREAM_MESSAGES[3] is None


@pytest.mark.parametrize("split", range(len(STREAM) + 1))
def test_cutting_the_stream_at_any_byte_gives_the_same_messages(split):
    assert messages_from(STREAM[:split], STREAM[split:]) == STREAM_MESSAGES


def test_feeding_one_byte_at_a_time_gives_the_same_messages():
    assert messages_from(*(STREAM[i : i + 1] for i in range(len(STREAM)))) == STREAM_MESSAGES


def random_value(rng: random.Random, depth: int = 0):
    kind = rng.randrange(7 if depth < 3 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return rng.randrange(-(10**12), 10**12)
    if kind == 2:
        return bytes(rng.randrange(256) for _ in range(rng.randrange(40)))
    if kind == 3:
        return SimpleString("".join(rng.choice("abcXYZ 019-_") for _ in range(rng.randrange(12))))
    if kind == 4:
        return RespError("ERR " + "".join(rng.choice("abc ") for _ in range(rng.randrange(8))))
    return [random_value(rng, depth + 1) for _ in range(rng.randrange(5))]


def test_random_values_survive_encoding_and_parsing_in_random_pieces():
    rng = random.Random(2024)
    values = [random_value(rng) for _ in range(300)]
    stream = b"".join(encode(v) for v in values)
    parser = Parser()
    found = []
    pos = 0

    while pos < len(stream):
        size = rng.randrange(1, 40)
        parser.feed(stream[pos : pos + size])
        pos += size
        found.extend(parser.messages())

    assert found == values
    assert not parser.pending


# ---- bad input ------------------------------------------------------------------------------


INVALID_INPUT = {
    "unknown type byte": b"?junk\r\n",
    "resp3 map is not supported": b"%1\r\n$1\r\na\r\n$1\r\nb\r\n",
    "integer with letters": b":abc\r\n",
    "empty integer": b":\r\n",
    "integer with plus sign": b":+5\r\n",
    "integer with space": b": 5\r\n",
    "integer with decimal point": b":5.0\r\n",
    "integer with 20 digits": b":" + b"9" * 20 + b"\r\n",
    "bulk length below -1": b"$-2\r\n",
    "bulk length not a number": b"$abc\r\n",
    "bulk length over the limit": b"$%d\r\n" % (MAX_BULK_LENGTH + 1),
    "bulk string without CRLF after it": b"$3\r\nabcXY",
    "array length below -1": b"*-5\r\n",
    "array length over the limit": b"*%d\r\n" % (MAX_ARRAY_LENGTH + 1),
    "arrays nested too deeply": b"*1\r\n" * (MAX_DEPTH + 4) + b":1\r\n",
    "endless line without CRLF": b"+" + b"a" * (MAX_LINE_LENGTH + 1),
    "too long line with CRLF": b"+" + b"a" * (MAX_LINE_LENGTH + 1) + b"\r\n",
}


@pytest.mark.parametrize("raw", INVALID_INPUT.values(), ids=INVALID_INPUT.keys())
def test_invalid_input_raises_protocol_error(raw):
    parser = Parser()
    parser.feed(raw)

    with pytest.raises(ProtocolError):
        list(parser.messages())


def test_messages_before_the_bad_bytes_are_still_delivered():
    parser = Parser()
    parser.feed(b"+OK\r\n:12\r\n:oops\r\n+never\r\n")
    delivered = []

    with pytest.raises(ProtocolError):
        for message in parser.messages():
            delivered.append(message)

    assert delivered == [SimpleString("OK"), 12]


def test_once_the_stream_is_corrupt_the_parser_keeps_refusing():
    parser = Parser()
    parser.feed(b"?\r\n")
    with pytest.raises(ProtocolError):
        list(parser.messages())

    parser.feed(b"+OK\r\n")

    with pytest.raises(ProtocolError):
        list(parser.messages())


def test_nesting_just_below_the_limit_is_fine():
    raw = b"*1\r\n" * (MAX_DEPTH - 2) + b":1\r\n"
    value = parse(raw)

    for _ in range(MAX_DEPTH - 2):
        assert isinstance(value, list)
        (value,) = value
    assert value == 1


def test_a_bare_newline_does_not_end_a_line():
    parser = Parser()
    parser.feed(b"+OK\n")

    assert list(parser.messages()) == []
    assert parser.pending


def test_a_hostile_giant_array_header_is_cheap_to_reject():
    parser = Parser()
    parser.feed(b"*999999999999\r\n")

    with pytest.raises(ProtocolError):
        list(parser.messages())


# ---- inline commands (plain text, like typing into telnet) ----------------------------------


def test_inline_commands_are_refused_by_default():
    parser = Parser()
    parser.feed(b"PING\r\n")

    with pytest.raises(ProtocolError):
        list(parser.messages())


def test_inline_commands_when_allowed():
    found = messages_from(b"PING\r\n", b"SET a  1\n", b"\r\n", b"  \r\n", allow_inline=True)

    assert found == [[b"PING"], [b"SET", b"a", b"1"]]


def test_inline_and_array_commands_can_be_mixed():
    stream = b"PING\r\n" + encode_command("GET", "a") + b"ECHO hi\r\n"

    assert messages_from(stream, allow_inline=True) == [
        [b"PING"],
        [b"GET", b"a"],
        [b"ECHO", b"hi"],
    ]


def test_an_inline_command_split_across_reads():
    assert messages_from(b"SE", b"T a ", b"1\r", b"\n", allow_inline=True) == [[b"SET", b"a", b"1"]]


def test_inline_line_without_a_newline_is_limited_in_length():
    parser = Parser(allow_inline=True)
    parser.feed(b"x" * (MAX_LINE_LENGTH + 1))

    with pytest.raises(ProtocolError):
        list(parser.messages())

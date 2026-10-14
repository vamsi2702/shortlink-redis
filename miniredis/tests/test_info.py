import time

import pytest
import redis

from miniredis.commands import ClientState, Context, Stats, execute
from miniredis.info import SECTIONS, human_bytes
from miniredis.store import Store


@pytest.fixture
def ctx(clock):
    return Context(store=Store(clock=clock), client=ClientState(id=1))


def run(ctx, *words):
    return execute(ctx, [w if isinstance(w, bytes) else str(w).encode() for w in words])


def parse(text: bytes) -> dict[str, dict[str, str]]:
    """INFO text -> {section: {field: value}}."""
    sections: dict[str, dict[str, str]] = {}
    current = None
    for line in text.decode().split("\r\n"):
        if line.startswith("# "):
            current = sections.setdefault(line[2:].lower(), {})
        elif line:
            field, _, value = line.partition(":")
            assert current is not None, line
            current[field] = value
    return sections


def info(client, *sections) -> dict[str, dict[str, str]]:
    return parse(client.call("INFO", *sections))


# ---- the report's layout --------------------------------------------------------------------


def test_the_report_has_every_section_in_a_fixed_order(ctx):
    text = run(ctx, "INFO")

    headers = [line for line in text.decode().split("\r\n") if line.startswith("# ")]
    assert headers == ["# Server", "# Clients", "# Memory", "# Stats", "# Keyspace"]


def test_sections_are_separated_by_a_blank_line_and_each_line_ends_with_crlf(ctx):
    text = run(ctx, "INFO").decode()

    assert "\r\n\r\n# Clients\r\n" in text
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")  # no bare newlines


def test_one_section_can_be_asked_for(ctx):
    sections = parse(run(ctx, "INFO", "clients"))

    assert list(sections) == ["clients"]
    assert sections["clients"]["connected_clients"] == "0"


def test_section_names_are_case_insensitive_and_come_out_in_the_fixed_order(ctx):
    sections = parse(run(ctx, "INFO", "STATS", "Server"))

    assert list(sections) == ["server", "stats"]


@pytest.mark.parametrize("word", ["all", "everything", "default", "ALL"])
def test_all_everything_and_default_mean_every_section(ctx, word):
    assert list(parse(run(ctx, "INFO", word))) == [*SECTIONS[:-1], "keyspace"]


def test_an_unknown_section_gives_an_empty_report_like_real_redis(ctx):
    assert run(ctx, "INFO", "bogus") == b""
    assert list(parse(run(ctx, "INFO", "bogus", "stats"))) == ["stats"]


def test_the_keyspace_section_is_just_a_header_when_there_are_no_keys(ctx):
    assert run(ctx, "INFO", "keyspace") == b"# Keyspace\r\n"


def test_the_keyspace_section_counts_keys_and_keys_with_an_expiry(ctx, clock):
    run(ctx, "SET", "a", 1)
    run(ctx, "SET", "b", 1, "EX", 100)
    run(ctx, "SET", "c", 1, "EX", 300)

    keyspace = parse(run(ctx, "INFO", "keyspace"))["keyspace"]

    assert keyspace["db0"] == "keys=3,expires=2,avg_ttl=200000"


# ---- the numbers ----------------------------------------------------------------------------


def test_commands_processed_counts_finished_commands_but_not_the_info_asking(ctx):
    run(ctx, "SET", "a", 1)
    run(ctx, "GET", "a")
    run(ctx, "FOO")  # unknown: never ran, so not counted
    run(ctx, "GET")  # wrong number of arguments: never ran

    def processed():
        return parse(run(ctx, "INFO", "stats"))["stats"]["total_commands_processed"]

    assert processed() == "2"
    assert processed() == "3"  # now the first INFO has been counted too


def test_a_command_that_ended_in_an_error_still_counts(ctx):
    run(ctx, "SET", "word", "hello")
    run(ctx, "INCR", "word")  # ran, and answered with an error

    assert ctx.stats.commands_processed == 2


def test_keyspace_hits_and_misses_count_reads(ctx):
    run(ctx, "SET", "a", 1)
    run(ctx, "GET", "a")
    run(ctx, "GET", "a")
    run(ctx, "GET", "nothing")
    run(ctx, "EXISTS", "a", "nothing")
    run(ctx, "TTL", "a")
    run(ctx, "LLEN", "nothing")
    run(ctx, "HGET", "nokey", "f")

    stats = parse(run(ctx, "INFO", "stats"))["stats"]

    assert (stats["keyspace_hits"], stats["keyspace_misses"]) == ("4", "4")


def test_writes_do_not_count_as_hits_or_misses(ctx):
    run(ctx, "SET", "a", 1)
    run(ctx, "INCR", "a")
    run(ctx, "INCR", "fresh")
    run(ctx, "LPUSH", "l", 1)
    run(ctx, "RPOP", "l")
    run(ctx, "HSET", "h", "f", 1)

    assert (ctx.stats.keyspace_hits, ctx.stats.keyspace_misses) == (0, 0)


def test_expired_keys_are_counted(ctx, clock):
    run(ctx, "SET", "a", 1, "PX", 10)
    run(ctx, "SET", "b", 1, "PX", 10)
    clock.advance(100)
    run(ctx, "GET", "a")  # found dead by a command
    ctx.store.purge_expired()  # found dead by the cleaner

    assert parse(run(ctx, "INFO", "stats"))["stats"]["expired_keys"] == "2"


def test_memory_grows_with_the_data(ctx):
    empty = ctx.store.memory_estimate()
    run(ctx, "SET", "big", "x" * 100_000)
    run(ctx, "LPUSH", "l", *["item"] * 100)
    run(ctx, "HSET", "h", *[str(i) for i in range(100)])

    used = int(parse(run(ctx, "INFO", "memory"))["memory"]["used_memory"])

    assert used > empty + 100_000


def test_human_readable_sizes():
    assert human_bytes(0) == "0B"
    assert human_bytes(1023) == "1023B"
    assert human_bytes(1024) == "1.00K"
    assert human_bytes(1536) == "1.50K"
    assert human_bytes(5 * 1024 * 1024) == "5.00M"
    assert human_bytes(3 * 1024**3) == "3.00G"
    assert human_bytes(2 * 1024**5) == "2048.00T"


def test_uptime_follows_the_clock_of_the_server(ctx):
    ctx.stats.started_at = time.time() - 90_000

    server = parse(run(ctx, "INFO", "server"))["server"]

    assert int(server["uptime_in_seconds"]) >= 90_000
    assert server["uptime_in_days"] == "1"


def test_the_servers_own_facts_are_added_to_a_section(clock):
    ctx = Context(
        store=Store(clock=clock),
        client=ClientState(id=1),
        stats=Stats(),
        extra_info=lambda: {"server": {"tcp_port": 1234, "redis_mode": "custom"}},
    )

    server = parse(run(ctx, "INFO"))["server"]

    assert server["tcp_port"] == "1234"
    assert server["redis_mode"] == "custom"  # a fact the server supplies replaces the default


def test_there_is_no_persistence_section(ctx):
    assert run(ctx, "INFO", "persistence") == b""  # an unknown section: an empty report
    assert "persistence" not in parse(run(ctx, "INFO"))


def test_info_on_a_big_dataset_still_answers_quickly(ctx):
    for i in range(50_000):
        run(ctx, "SET", f"key{i}", "value" * 5)

    start = time.perf_counter()
    text = run(ctx, "INFO")
    elapsed = time.perf_counter() - start

    assert parse(text)["keyspace"]["db0"].startswith("keys=50000")
    assert elapsed < 2.0


# ---- through the real server ----------------------------------------------------------------


def wait_for_connected_clients(client, expected: int, timeout: float = 3.0) -> None:
    """The count is right 'soon', not instantly: the server may serve one client before it has
    got round to accepting (or noticing the departure of) another."""
    deadline = time.monotonic() + timeout
    while True:
        current = info(client, "clients")["clients"]["connected_clients"]
        if current == str(expected):
            return
        assert time.monotonic() < deadline, f"connected_clients stayed at {current}"
        time.sleep(0.02)


def test_connected_clients_follows_connections_coming_and_going(connect):
    first = connect()
    wait_for_connected_clients(first, 1)
    second = connect()
    wait_for_connected_clients(first, 2)

    second.close()

    wait_for_connected_clients(first, 1)


def test_total_connections_and_commands_over_a_real_connection(connect):
    client = connect()
    for i in range(5):
        client.call("SET", f"k{i}", i)

    stats = info(client, "stats")["stats"]

    assert stats["total_connections_received"] == "1"
    assert stats["total_commands_processed"] == "5"
    assert info(connect(), "stats")["stats"]["total_connections_received"] == "2"


def test_the_servers_real_port_and_process_are_reported(server, client):
    sections = info(client, "server")

    assert sections["server"]["tcp_port"] == str(server.address[1])
    assert int(sections["server"]["process_id"]) > 0
    assert sections["server"]["redis_mode"] == "standalone"


def test_redis_py_can_read_the_report(server):
    host, port = server.address
    with redis.Redis(host=host, port=port, protocol=2) as r:
        r.set("a", 1)
        r.set("b", 2, ex=100)

        everything = r.info()
        keyspace = r.info("keyspace")

    assert everything["connected_clients"] == 1
    assert everything["redis_mode"] == "standalone"
    assert everything["total_commands_processed"] >= 2
    assert keyspace["db0"]["keys"] == 2
    assert keyspace["db0"]["expires"] == 1


def test_info_matches_real_redis_where_it_can(connect, connect_real):
    ours, real = connect(), connect_real()

    assert ours.call("INFO", "bogus") == real.call("INFO", "bogus") == b""
    for section in ("clients", "memory", "stats"):
        ours_fields = info(ours, section)[section]
        real_fields = info(real, section)[section]
        shared = set(ours_fields) & set(real_fields)
        assert shared, section  # we use the field names real Redis uses
        for field in shared:
            if not field.endswith("_human"):  # numbers are numbers in both
                assert ours_fields[field].lstrip("-").isdigit(), field
                assert real_fields[field].lstrip("-").isdigit(), field
    # the sections that exist in both come in the same relative order
    real_sections = [name for name in info(real) if name in SECTIONS]
    assert real_sections == [name for name in SECTIONS if name in real_sections]

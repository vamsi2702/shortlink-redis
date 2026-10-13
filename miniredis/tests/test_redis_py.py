import threading

import pytest
import redis


@pytest.fixture
def r(server):
    """redis-py, the real client library, pointed at MiniRedis. protocol=2: see test below."""
    host, port = server.address
    connection = redis.Redis(host=host, port=port, protocol=2)
    yield connection
    connection.close()


def test_ping(r):
    assert r.ping() is True


def test_set_and_get(r):
    assert r.set("name", "vamsi") is True
    assert r.get("name") == b"vamsi"
    assert r.get("missing") is None


def test_the_step_2_7_goal_redis_py_can_set_and_get(r):
    r.set("a", 1)

    assert r.get("a") == b"1"


def test_incr_and_incr_by_an_amount(r):
    assert r.incr("counter") == 1  # redis-py sends INCRBY counter 1
    assert r.incr("counter", 5) == 6
    assert r.incr("counter", -2) == 4
    assert r.get("counter") == b"4"


def test_incr_on_text_raises_the_same_error_real_redis_gives(r):
    r.set("word", "hello")

    with pytest.raises(redis.ResponseError, match="value is not an integer or out of range"):
        r.incr("word")


def test_delete_and_exists_count_keys(r):
    r.set("a", 1)
    r.set("b", 2)

    assert r.exists("a", "b", "nothing") == 2
    assert r.delete("a", "b", "nothing") == 2
    assert r.exists("a") == 0


def test_echo(r):
    assert r.echo("hi") == b"hi"


def test_binary_values_survive(r):
    value = bytes(range(256)) * 10

    r.set(b"binary \x00 key", value)

    assert r.get(b"binary \x00 key") == value


def test_decode_responses_gives_text_back(server):
    host, port = server.address
    with redis.Redis(host=host, port=port, protocol=2, decode_responses=True) as text_client:
        text_client.set("greeting", "héllo ✓")

        assert text_client.get("greeting") == "héllo ✓"


def test_client_name_and_id(r):
    assert r.client_getname() is None
    assert r.client_setname("demo") is True
    assert r.client_getname() == "demo"  # redis-py decodes this one for us
    assert isinstance(r.client_id(), int)


def test_unknown_commands_raise_response_error_not_a_crash(r):
    with pytest.raises(redis.ResponseError, match="unknown command 'FOO'"):
        r.execute_command("FOO", "bar")
    with pytest.raises(redis.ResponseError, match="unknown command 'DECRBY'"):
        r.decr("x")  # not implemented yet
    assert r.ping() is True  # the connection is still fine


def test_a_pipeline_sends_many_commands_in_one_go(r):
    pipe = r.pipeline(transaction=False)
    for i in range(100):
        pipe.set(f"k{i}", i)
    pipe.get("k42")
    pipe.incr("n")
    results = pipe.execute()

    assert results[:100] == [True] * 100
    assert results[100:] == [b"42", 1]


def test_many_threads_sharing_one_client_pool(r):
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(100):
                r.incr("shared")
        except Exception as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert r.get("shared") == b"1000"


def test_the_connection_is_closed_after_quit_and_redis_py_reconnects(r):
    assert r.set("before", "1") is True

    assert r.execute_command("QUIT") is True

    assert r.get("before") == b"1"  # a new connection, same shared store


def test_hello_2_works_and_describes_the_server(r):
    reply = r.execute_command("HELLO", 2)

    assert len(reply) == 14


def test_a_client_asking_for_resp3_gets_a_clear_error(server):
    """MiniRedis only speaks RESP2. redis-py does not fall back by itself, so the API's client
    must be created with protocol=2."""
    host, port = server.address
    resp3_client = redis.Redis(host=host, port=port, protocol=3)

    with pytest.raises(redis.ResponseError, match="NOPROTO"):
        resp3_client.ping()
    resp3_client.close()

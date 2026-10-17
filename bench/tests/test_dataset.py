import psycopg
import pytest

from bench import dataset, seed

TEST_DATABASE = "shortlink_bench_test"
LINKS_QUERY = "SELECT id, code, long_url, user_id FROM links ORDER BY id"


# ---- the data as pure functions -----------------------------------------------------------------


def test_codes_are_unique_six_character_base62():
    codes = dataset.make_codes(5000)

    assert len(codes) == len(set(codes)) == 5000
    assert all(len(code) == 6 and code.isalnum() and code.isascii() for code in codes)


def test_the_same_seed_gives_the_same_codes():
    assert dataset.make_codes(300, seed=7) == dataset.make_codes(300, seed=7)


def test_another_seed_gives_other_codes():
    assert dataset.make_codes(300, seed=7) != dataset.make_codes(300, seed=8)


def test_a_smaller_dataset_is_the_start_of_a_larger_one():
    assert dataset.make_codes(50) == dataset.make_codes(500)[:50]


def test_the_dataset_never_changes_between_machines_or_python_versions():
    """A pinned value. If this fails, the data changed and old benchmark results do not compare."""
    assert dataset.fingerprint(dataset.make_codes(1000)) == "26a524da575c0c6c"


def test_links_are_shared_evenly_between_users():
    owners = [dataset.owner(index, users=10) for index in range(1000)]

    assert set(owners) == set(range(1, 11))
    assert owners.count(1) == owners.count(10) == 100


def test_urls_are_made_up_but_valid():
    assert dataset.long_url(42) == "https://example.com/page/42"


# ---- loading it into Postgres -------------------------------------------------------------------


@pytest.fixture
def test_database():
    yield TEST_DATABASE
    with psycopg.connect(seed._conninfo("postgres"), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)')


def count(database, table):
    with psycopg.connect(seed._conninfo(database)) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_seeding_loads_the_dataset(test_database):
    fingerprint = seed.seed_database(test_database, links=500, users=7, seed=1)

    assert count(test_database, "links") == 500
    assert count(test_database, "users") == 7
    assert count(test_database, "clicks") == 0
    assert fingerprint == dataset.fingerprint(dataset.make_codes(500, 1))


def test_seeding_twice_gives_the_same_database(test_database):
    seed.seed_database(test_database, links=300, users=5, seed=3)
    with psycopg.connect(seed._conninfo(test_database)) as conn:
        first = conn.execute(LINKS_QUERY).fetchall()
        conn.execute("INSERT INTO clicks (link_id) VALUES (1)")  # leftovers from a benchmark run

    seed.seed_database(test_database, links=300, users=5, seed=3)

    with psycopg.connect(seed._conninfo(test_database)) as conn:
        second = conn.execute(LINKS_QUERY).fetchall()
    assert first == second  # same ids, codes, urls and owners
    assert count(test_database, "clicks") == 0  # and the old clicks are gone


def test_it_refuses_to_empty_a_database_that_is_not_a_benchmark_one():
    with pytest.raises(SystemExit, match="refusing"):
        seed.seed_database("shortlink", links=10, users=1, seed=1)

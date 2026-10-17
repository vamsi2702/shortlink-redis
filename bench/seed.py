"""Build the benchmark database. One command, same result every time:

    python -m bench.seed        # 100,000 links and 100 users in the `shortlink_bench` database

It uses a separate database (never the development one), creates it if needed, applies the real
migrations, empties the tables and loads the data. Postgres must be running (docker compose up -d).
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import bcrypt
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url

from api import config
from bench import dataset

BENCH_DATABASE = "shortlink_bench"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# a fixed salt, so the placeholder password hash is the same every run; nobody ever logs in as these
_SALT = b"$2b$04$abcdefghijklmnopqrstuu"
PASSWORD_HASH = bcrypt.hashpw(b"benchmark-password", _SALT).decode()


def database_url(database: str) -> str:
    """The development DATABASE_URL, pointed at another database on the same server."""
    url = make_url(config.DATABASE_URL).set(database=database)
    return url.render_as_string(hide_password=False)


def _conninfo(database: str) -> str:
    url = make_url(config.DATABASE_URL)
    return make_conninfo(
        host=url.host, port=url.port, user=url.username, password=url.password, dbname=database
    )


def create_database(database: str) -> None:
    with psycopg.connect(_conninfo("postgres"), autocommit=True) as conn:
        query = "SELECT 1 FROM pg_database WHERE datname = %s"
        if not conn.execute(query, (database,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


def migrate(database: str) -> None:
    """Create the tables with the project's real migrations (alembic reads DATABASE_URL)."""
    env = {**os.environ, "DATABASE_URL": database_url(database)}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
    )


def load(database: str, links: int, users: int, seed: int) -> str:
    """Empty the tables and load the dataset. Returns the dataset's fingerprint."""
    codes = dataset.make_codes(links, seed)
    with psycopg.connect(_conninfo(database)) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE clicks, links, users RESTART IDENTITY CASCADE")
        with cur.copy("COPY users (email, password_hash) FROM STDIN") as copy:
            for number in range(1, users + 1):
                copy.write_row((f"bench{number}@example.com", PASSWORD_HASH))
        with cur.copy("COPY links (code, long_url, user_id) FROM STDIN") as copy:
            for index, code in enumerate(codes):
                copy.write_row((code, dataset.long_url(index), dataset.owner(index, users)))
        cur.execute("ANALYZE links")
    return dataset.fingerprint(codes)


def seed_database(database: str, links: int, users: int, seed: int) -> str:
    if "bench" not in database:
        raise SystemExit(f"refusing to empty {database!r}: the name must contain 'bench'")
    create_database(database)
    migrate(database)
    return load(database, links, users, seed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the benchmark database")
    parser.add_argument("--links", type=int, default=dataset.DEFAULT_LINKS)
    parser.add_argument("--users", type=int, default=dataset.DEFAULT_USERS)
    parser.add_argument("--seed", type=int, default=dataset.DEFAULT_SEED)
    parser.add_argument("--database", default=BENCH_DATABASE)
    args = parser.parse_args()

    start = time.perf_counter()
    fingerprint = seed_database(args.database, args.links, args.users, args.seed)
    print(
        f"database {args.database}: {args.links:,} links, {args.users} users, seed {args.seed}, "
        f"dataset {fingerprint} ({time.perf_counter() - start:.1f}s)"
    )


if __name__ == "__main__":
    main()

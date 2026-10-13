# ShortLink + MiniRedis

A URL shortener backend (FastAPI + PostgreSQL) that will use a Redis-compatible cache server written from scratch in Python.

Work in progress. See [PLAN.md](PLAN.md) for the roadmap and [docs/DIAGRAMS.md](docs/DIAGRAMS.md) for diagrams.

**Status:** Phase 1 (the backend) and Phases 2-3 (MiniRedis: protocol, commands, expiry, lists, hashes, persistence, Pub/Sub) are complete. The API still uses an in-process cache behind the same interface; swapping in MiniRedis is Phase 4.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
docker compose up -d            # starts PostgreSQL
alembic upgrade head            # creates the tables
uvicorn api.main:app --reload   # http://127.0.0.1:8000  (interactive docs at /docs)
```

Run the tests (PostgreSQL must be running):

```powershell
pytest                              # all tests
pytest --cov=api --cov-report=term-missing   # with coverage
ruff check .                        # lint
```

## MiniRedis

A Redis-compatible server written from scratch (see [PLAN.md](PLAN.md), Phases 2 and 3).

```powershell
python -m miniredis.server            # listens on 127.0.0.1:6380
```

It speaks RESP2, so `redis-cli` and `redis-py` work. With `redis-py`, **use `protocol=2`**: its
default asks for RESP3 first, and MiniRedis answers `NOPROTO unsupported protocol version`.

```python
import redis
r = redis.Redis(port=6380, protocol=2)
r.set("name", "vamsi")
r.get("name")          # b'vamsi'
r.incr("hits")         # 1
```

| Area | Commands |
|---|---|
| Strings | `SET` (with `EX PX EXAT PXAT NX XX KEEPTTL`), `GET`, `DEL`, `EXISTS`, `INCR`, `INCRBY` |
| Expiry | `EXPIRE PEXPIRE EXPIREAT PEXPIREAT` (with `NX XX GT LT`), `TTL`, `PTTL`, `PERSIST` |
| Lists | `LPUSH`, `RPOP` (with a count), `LLEN`, `LRANGE` |
| Hashes | `HSET`, `HGET`, `HGETALL` |
| Pub/Sub | `SUBSCRIBE`, `UNSUBSCRIBE`, `PUBLISH` |
| Server | `INFO`, `BGREWRITEAOF`, `TYPE`, `PING`, `ECHO`, `CLIENT`, `HELLO 2`, `COMMAND`, `QUIT` |

**Persistence.** Every change is appended to a log file (`data/miniredis.aof` by default) before
the reply is sent, and replayed when the server starts, so a restart or a `kill -9` loses nothing
that was acknowledged. Expiry times are stored as exact moments, so a key keeps counting down
while the server is off. A log cut off or damaged at its end is repaired on start-up (the
discarded bytes are saved in `<file>.discarded`).

```powershell
python -m miniredis.server --appendfsync always     # always | everysec (default) | no
python -m miniredis.server --no-aof                 # memory only
python -m miniredis.server --aof-file my.aof --auto-aof-rewrite-min-size 1048576
```

`--appendfsync` chooses how often the file is forced to the disk: `always` is safest (and
slowest), `everysec` can lose about a second of changes if the *machine* loses power, `no` leaves
it to the operating system. The log is compacted by `BGREWRITEAOF`, or by itself once it has
grown a lot; MiniRedis does this on its event loop, so clients wait about half a second per
100,000 keys.

Replies are checked against real Redis by the tests in
`miniredis/tests/test_compat_real_redis.py` and `test_pubsub.py` (run `docker compose up -d redis`
to enable them).

Not implemented: `SET ... GET`, RESP3 (`HELLO 3`), pattern subscriptions, transactions, scripting,
replication, `FLUSHALL`, eviction when memory is full.

## API

| Method and path | Auth | What it does |
|---|---|---|
| `GET /` | none | A basic web page: shorten links, sign in, manage your links |
| `GET /health` | none | Liveness check |
| `POST /auth/register` | none | Create an account (`email`, `password` of 8-72 bytes) |
| `POST /auth/login` | none | Returns a bearer token (valid 60 minutes) |
| `GET /auth/me` | token | The logged-in user |
| `POST /shorten` | optional | Body `{"url": "..."}`. With a token, the link belongs to you |
| `GET /links` | token | Your links, newest first (`limit`, `offset`) |
| `PATCH /links/{code}` | token | Change where your link points |
| `DELETE /links/{code}` | token | Delete your link and its clicks |
| `GET /{code}` | none | 302 redirect to the long URL, or 404 |

Send the token as `Authorization: Bearer <token>`. In `/docs`, use the **Authorize** button and paste the token.

## How it works

- **Short codes:** 6 random base62 characters (about 56 billion combinations). Collisions are settled by the database's unique constraint, with a retry.
- **URL validation:** only `http`/`https`; no credentials in the URL; no localhost, private or reserved addresses. No DNS lookup is done, so a public name that points to a private address is not caught.
- **Caching:** redirects use cache-aside. Lookups hit the cache first (5 minute TTL), then Postgres. Unknown codes are cached for 30 seconds. Creating, editing and deleting a link clears its entry.
- **Auth:** passwords are hashed with bcrypt; login returns a signed JWT. Other people's links return 404, not 403, so their existence is not revealed.
- **Redirect status:** 302, not 301, so browsers keep asking the server and clicks can be counted later.

## Configuration

Environment variables, all optional for local development: `DATABASE_URL`, `BASE_URL`, `JWT_SECRET` (**must be set in any real deployment**), `JWT_EXPIRE_MINUTES`, `BCRYPT_ROUNDS`, `CACHE_TTL_SECONDS`, `NOT_FOUND_TTL_SECONDS`.

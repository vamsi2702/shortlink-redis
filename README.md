# ShortLink + MiniRedis

A URL shortener backend (FastAPI + PostgreSQL) whose cache, rate limiter and click queue run on a Redis-compatible server written from scratch in Python (MiniRedis).

Work in progress. See [PLAN.md](PLAN.md) for the roadmap and [docs/DIAGRAMS.md](docs/DIAGRAMS.md) for diagrams.

**Status:** Phase 1 (the backend), Phases 2-3 (MiniRedis: protocol, commands, expiry, lists, hashes, Pub/Sub) and Phase 4 (the API uses MiniRedis for caching, rate limiting and a click queue, with a click worker and `/metrics`) are complete. Phase 5 (measuring it) is next.

## Run everything

```powershell
docker compose up -d --build   # PostgreSQL, MiniRedis, the API, the click worker (tables are created for you)
```

The API is then at http://localhost:8000 (interactive docs at `/docs`, numbers at `/metrics`).
`docker compose down` stops it; the database keeps its data, MiniRedis does not (it is memory only).

```
browser ──► API ──► MiniRedis   cache (GET/SET), rate limit (SET NX + INCR), click queue (LPUSH)
             │          ▲
             ▼          │ RPOP in batches
         PostgreSQL ◄── click worker
```

## Setup for development

Run only the database in Docker and the rest on your machine:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
docker compose up -d postgres   # starts PostgreSQL
alembic upgrade head            # creates the tables
uvicorn api.main:app --reload   # http://127.0.0.1:8000  (interactive docs at /docs)
```

That uses the API's own in-process cache, and clicks are not recorded. To use MiniRedis too, start it
and the worker in two more terminals and point the API at it:

```powershell
python -m miniredis.server                     # 127.0.0.1:6380
$env:MINIREDIS_URL = "redis://127.0.0.1:6380"  # set this before starting uvicorn and the worker
python -m api.worker                           # saves queued clicks to PostgreSQL
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
| Server | `INFO`, `TYPE`, `PING`, `ECHO`, `CLIENT`, `HELLO 2`, `COMMAND`, `QUIT` |

**Memory only.** MiniRedis keeps all its data in memory, like a cache: when the server stops, the
data is gone. Expired keys are removed both when they are touched and by a background cleaner.
(An append-only-file persistence feature was built and then removed again; it is in the git
history before the commit "Remove append-only file persistence" if it is ever wanted back.)

Replies are checked against real Redis by the tests in
`miniredis/tests/test_compat_real_redis.py` and `test_pubsub.py` (run `docker compose up -d redis`
to enable them).

Not implemented: persistence to disk, `SET ... GET`, RESP3 (`HELLO 3`), pattern subscriptions,
transactions, scripting, replication, `FLUSHALL`, eviction when memory is full.

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
| `GET /metrics` | none | Request counts, cache hits and misses, latency (Prometheus text format). Keep it off the public internet |

Redirects, `POST /shorten` and `/auth/register` + `/auth/login` are rate limited: past 100 requests
a minute from one client (the signed-in user, or else the IP address), they answer `429` with a
`Retry-After` header. Each of the three groups has its own count.

Send the token as `Authorization: Bearer <token>`. In `/docs`, use the **Authorize** button and paste the token.

## How it works

- **Short codes:** 6 random base62 characters (about 56 billion combinations). Collisions are settled by the database's unique constraint, with a retry.
- **URL validation:** only `http`/`https`; no credentials in the URL; no localhost, private or reserved addresses. No DNS lookup is done, so a public name that points to a private address is not caught.
- **Caching:** redirects use cache-aside. Lookups hit the cache first (5 minute TTL), then Postgres. Unknown codes are cached for 30 seconds. Creating, editing and deleting a link clears its entry.
- **Auth:** passwords are hashed with bcrypt; login returns a signed JWT. Other people's links return 404, not 403, so their existence is not revealed.
- **Redirect status:** 302, not 301, so browsers keep asking the server and clicks can be counted.
- **Rate limiting:** a fixed window per client: `SET key 0 NX EX 60`, then `INCR`. Past the limit the answer is 429. If MiniRedis is down the limiter lets requests through ("fails open") instead of taking the site down.
- **Click queue:** a redirect only does one `LPUSH clicks "<code> <time>"` into MiniRedis. The worker takes up to 100 events at a time (`RPOP clicks 100`) and writes them to the `clicks` table with one INSERT. If the database write fails, the events are put back.
- **When MiniRedis is down:** redirects fall back to Postgres, and after one failure the API stops trying MiniRedis for 5 seconds so the dead server does not slow every request. Clicks queued at that moment, and clicks still waiting in a MiniRedis that restarts, are lost (it is memory only).
- **Metrics:** `/metrics` counts requests by route *template* (`/{code}`, not each real code), and records cache hits and misses, 429s, queued and dropped clicks, and MiniRedis failures. The numbers are per process and reset on restart.

## Configuration

Environment variables, all optional for local development:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | local Postgres | Where the data lives |
| `BASE_URL` | `http://localhost:8000` | Short links are `BASE_URL/<code>` |
| `JWT_SECRET` | dev-only value | **Must be set in any real deployment** |
| `JWT_EXPIRE_MINUTES`, `BCRYPT_ROUNDS` | 60, 12 | Login token life, password hashing cost |
| `MINIREDIS_URL` | empty | e.g. `redis://localhost:6380`. Empty: use the in-process cache; clicks are not recorded |
| `MINIREDIS_TIMEOUT_SECONDS` | 0.25 | A MiniRedis call slower than this counts as failed |
| `MINIREDIS_RETRY_SECONDS` | 5 | After a failure, wait this long before trying MiniRedis again |
| `CACHE_TTL_SECONDS`, `NOT_FOUND_TTL_SECONDS` | 300, 30 | How long a redirect, or an unknown code, stays cached |
| `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW_SECONDS` | 100, 60 | The rate limit |
| `CLICK_QUEUE_KEY`, `CLICK_BATCH_SIZE`, `CLICK_WORKER_INTERVAL_SECONDS` | clicks, 100, 1 | The click queue and how the worker drains it |

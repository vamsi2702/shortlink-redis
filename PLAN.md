# ShortLink + MiniRedis — Project Plan

A URL shortener backend (FastAPI + PostgreSQL) that uses a **Redis-compatible cache server written from scratch in Python** for caching and rate limiting.

## 1. Goals

| Goal | How this project covers it |
|---|---|
| Multithreading / concurrency | asyncio TCP server handling many clients, background workers, optional threaded server variant for comparison |
| Networking | RESP protocol over raw TCP sockets, HTTP API |
| Backend | REST API, database, auth, caching, rate limiting, deployment |
| Resume value | Measurable benchmarks, tests, CI, Docker, clear README |

## 2. Architecture

```
Client ──HTTP──> FastAPI service ──SQL──> PostgreSQL
                      │
                      └──RESP/TCP──> MiniRedis (our own server)
                                        ├─ key-value store (dict)
                                        ├─ TTL expiry (heap)
                                        ├─ AOF persistence
                                        └─ Pub/Sub
```

- **API service**: creates short links, redirects, rate-limits, records clicks.
- **MiniRedis**: cache for hot redirects, counters for rate limiting, list queue for click events (Pub/Sub kept as a demo feature only).
- **PostgreSQL**: source of truth for links, users, analytics.

### Design decisions (fixed up front)

- **Click events use a list queue** (`LPUSH` / `RPOP`), not Pub/Sub. Pub/Sub drops messages when no subscriber is connected; a queue does not.
- **Failure behavior:** if MiniRedis is down, redirects fall back to Postgres and rate limiting fails open. This is tested.
- **Cache rules:** fixed cache TTL; invalidate the cache key when a link is edited or deleted; cache "not found" briefly (short TTL) to absorb junk-code traffic.
- **Atomic rate limiting:** `SET key 1 EX <window> NX`, then `INCR` on later hits, so a counter can never be left without a TTL.
- **URL validation:** accept only `http`/`https`; reject `localhost`, private and link-local IPs, and overly long URLs.

## 3. Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ (developed on 3.14) |
| API | FastAPI, Uvicorn |
| Database | PostgreSQL, SQLAlchemy 2.x, Alembic |
| Cache server | Custom, `asyncio` streams, `dict` + `heapq` |
| Auth | JWT (`pyjwt`), password hashing (`bcrypt`; `passlib` is unmaintained) |
| Testing | pytest, pytest-asyncio, redis-py (as a compatibility client) |
| Load testing | locust, or a custom asyncio script; `redis-benchmark` for raw cache numbers |
| Packaging / infra | Docker, docker-compose, GitHub Actions |
| Deployment (Phase 6) | AWS (EC2, RDS, ECR, Route 53, CloudWatch), Terraform, Kubernetes (kind/minikube, optionally EKS), Prometheus + Grafana |
| Lint / types | ruff, mypy |

Environment: Windows 11. Docker Desktop (WSL2) is recommended for Postgres and compose. WSL2 Ubuntu is optional but useful. **Run MiniRedis in Docker (Linux) for all benchmarks** to avoid Windows event-loop differences and to keep the comparison with real Redis fair.

## 4. Repository layout (target)

```
shortlink-redis/
├─ PLAN.md
├─ README.md
├─ docker-compose.yml
├─ miniredis/            # cache server (own package)
│  ├─ server.py
│  ├─ resp.py            # protocol parser/encoder
│  ├─ commands.py
│  ├─ store.py           # dict + TTL
│  ├─ aof.py             # persistence
│  └─ tests/
├─ api/                  # FastAPI service
│  ├─ main.py
│  ├─ models.py
│  ├─ routes/
│  ├─ services/
│  ├─ migrations/
│  └─ tests/
├─ bench/                # load tests, dataset seeding, benchmark scripts
│  ├─ seed.py            # generates the synthetic link dataset
│  └─ workload.py        # Zipf-distributed request generator
├─ deploy/               # Phase 6
│  ├─ terraform/         # AWS infrastructure as code
│  └─ k8s/               # Kubernetes manifests or Helm chart
└─ .github/workflows/    # ci.yml, deploy.yml
```

## 5. Phases and milestones

Estimated 6–8 weeks part-time, but **go at your own pace**. Every phase is split into small steps (e.g. 1.1, 1.2). Each step is meant to take one or two sittings, ends with something you can run or see, and has a "Done when" check. Do not start a step until the previous one is done. If a step feels too big, split it again.

**Rule for beginners:** before each phase, spend a little time on the "Learn first" list, then build. Commit to git at the end of every step.

### Phase 0 — Setup

**0.1 Tools**
- Learn first: what a terminal, git and a virtual environment are.
- [x] Install Python 3.12+, Git, VS Code, Docker Desktop
- **Done when:** `python --version`, `git --version`, `docker --version` all print versions.

**0.2 Project skeleton**
- [x] Rename folder, create the repo layout, `git init`
- [ ] First commit
- [x] Create a venv and `pyproject.toml`
- **Done when:** the venv activates and the first commit exists.

**0.3 Tests and linting**
- [x] Install pytest and ruff; write one dummy test
- **Done when:** `pytest` passes and `ruff check` runs.

**0.4 Database**
- [x] `docker-compose.yml` with Postgres; connect with any client
- **Done when:** Postgres is reachable from Python.

### Phase 1 — Backend first, with a plain cache

**1.1 Hello API**
- Learn first: what HTTP, GET/POST and JSON are; FastAPI basics.
- [x] FastAPI app with `/health`
- **Done when:** `/health` returns OK in the browser and there is a test for it.

**1.2 Database and models**
- Learn first: SQL basics (tables, rows, primary keys); SQLAlchemy models.
- [x] Models: `User`, `Link`, `Click`
- [x] Alembic migration
- **Done when:** the tables exist in Postgres and you can insert and read a row from Python.

**1.3 Shorten a link**
- [x] `POST /shorten` with random base62 code
- [x] URL validation (http/https only, no localhost/private IPs)
- **Done when:** posting a URL returns a code, and bad URLs are rejected. Tests written.

**1.4 Redirect**
- [x] `GET /{code}` returns a 302 redirect; unknown code returns 404
- **Done when:** the short link opens the long URL in your browser.

**1.5 Users and auth**
- Learn first: password hashing and what a JWT is.
- [x] Register and login endpoints, JWT
- **Done when:** you can register, log in and receive a token.

**1.6 Per-user links**
- [x] Links belong to a user; list, edit and delete your own links
- **Done when:** one user cannot see or change another user's links.

**1.7 Plain cache**
- Learn first: what a cache is and why it is faster.
- [x] In-process dict cache behind a small `Cache` interface (get / set / delete / incr / expire)
- [x] Cache invalidation on edit/delete; short-TTL negative caching
- **Done when:** the second redirect for a code does not hit Postgres, and editing a link clears the cache.

**1.8 Test sweep**
- [x] Tests for all endpoints
- **Done when:** you can create and follow short links locally with all tests passing. *(Milestone: you now have a complete backend. MiniRedis is a bonus from here on.)*

### Phase 2 — MiniRedis core

**2.1 TCP basics**
- Learn first: what a socket is, client vs server, what TCP is.
- [x] Plain blocking echo server and client with `socket`
- **Done when:** the client sends text and the server echoes it back.

**2.2 Async echo server**
- Learn first: `async`/`await`, the event loop.
- [x] Rewrite the echo server with `asyncio` streams
- **Done when:** 50 clients can connect at once and all get replies.

**2.3 Understand RESP**
- Learn first: read the RESP spec on redis.io (simple strings, errors, integers, bulk strings, arrays).
- [x] Run real Redis in Docker; use `redis-cli` and watch the raw traffic
- **Done when:** you can write out by hand what `SET a 1` looks like on the wire.

**2.4 RESP parser and encoder**
- [x] `resp.py` parses and encodes all five types
- [x] Unit tests, including partial data and split packets
- **Done when:** the tests pass, including a message that arrives in two pieces.

**2.5 First commands**
- [x] Command dispatcher; `PING`, `SET`, `GET`
- **Done when:** `redis-cli -p <port> set a 1` and `get a` work against your server.

**2.6 More commands**
- [x] `DEL`, `EXISTS`, `INCR`, `INCRBY` (`redis-py` sends `INCRBY key 1` for `incr()`), plus error replies for unknown commands and wrong arguments, copied from [docs/RESP.md](docs/RESP.md)
- **Done when:** each command has a test.

**2.7 redis-py compatibility**
- [x] Handshake: `CLIENT SETINFO`/`SETNAME` reply `+OK`; `HELLO 2` replies like real Redis and any other version gets `-NOPROTO`; `COMMAND`. The API's client uses `protocol=2`, so it never sends `HELLO` (see [docs/RESP.md](docs/RESP.md))
- **Done when:** `redis-py` can `set` and `get` against your server.

### Phase 3 — MiniRedis features

**3.1 Key expiry, lazy**
- [x] `EXPIRE`, `TTL`, `SET ... EX [NX]`; expired keys are removed when accessed
- **Done when:** a key with a 2-second TTL is gone after 3 seconds.

**3.2 Key expiry, background**
- Learn first: heaps (`heapq`).
- [x] Background task removes expired keys using a heap
- **Done when:** expired keys are removed even if nobody reads them.

**3.3 Lists**
- [x] `LPUSH`, `RPOP` (and `LLEN`)
- **Done when:** a producer and a consumer can share a queue.

**3.4 Hashes**
- [x] `HSET`, `HGET`
- **Done when:** tests pass.

**3.5 AOF write**
- Learn first: what append-only logs and fsync are.
- [ ] Log every write command to a file; choose an fsync policy
- **Done when:** the file grows as you send writes.

**3.6 AOF replay**
- [ ] Replay the log on startup
- **Done when:** data survives a restart, and expired keys stay expired.

**3.7 Crash safety**
- [ ] Tests: kill mid-write, truncated file, corrupted last entry
- **Done when:** the server starts cleanly after each of these.

**3.8 `INFO` command**
- [ ] Ops count, connected clients, key count, memory estimate
- **Done when:** `INFO` returns readable stats.

**3.9 (Optional) AOF compaction**
- [ ] Rewrite the log so it holds only the current state
- **Done when:** the file shrinks and replay still gives the same data.

**3.10 (Optional) Pub/Sub**
- [ ] `SUBSCRIBE`, `PUBLISH`
- **Done when:** two subscribers both receive a published message. *(First thing to cut if you are behind.)*

### Phase 4 — Integrate

**4.1 MiniRedis client in the API**
- [ ] Implement the `Cache` interface on top of `redis-py` pointed at MiniRedis
- **Done when:** the API redirect cache is served from MiniRedis, and all Phase 1 tests still pass.

**4.2 Fallback on failure**
- [ ] If MiniRedis is down, redirects fall back to Postgres
- **Done when:** stopping MiniRedis does not break redirects.

**4.3 Rate limiting**
- [ ] Fixed/sliding window via `SET NX EX` + `INCR` (or a token bucket); returns 429; fails open if MiniRedis is down
- **Done when:** the 101st request in a minute gets 429 and the counter resets afterwards.

**4.4 Click queue**
- [ ] On each redirect, `LPUSH` a click event
- **Done when:** events appear in the list.

**4.5 Click worker**
- [ ] Background worker drains the list and writes analytics to Postgres in batches
- **Done when:** click counts in Postgres match the number of redirects.

**4.6 Metrics**
- [ ] `/metrics` endpoint: request counts, cache hits/misses, latency
- **Done when:** the numbers change as you make requests.

**4.7 Compose everything**
- [ ] `docker-compose.yml` runs API, Postgres and MiniRedis together
- **Done when:** `docker compose up` gives a working system end to end.

### Phase 5 — Measure and polish

**5.1 Dataset**
- [ ] `bench/seed.py` generates 100k links (see section 6)
- **Done when:** one command rebuilds the same dataset.

**5.2 Workload generator**
- [ ] `bench/workload.py` with Zipf and uniform access patterns
- **Done when:** it produces a request stream and a first single-client test runs.

**5.3 Load test, three setups**
- [ ] Redirects with cache off / real Redis / MiniRedis; record the metrics in section 7
- **Done when:** you have a results table with p50, p95, p99 and ops/sec.

**5.4 Reliability tests**
- [ ] Kill MiniRedis under load; measure recovery time and click-event loss
- **Done when:** the numbers are recorded.

**5.5 Concurrency experiment**
- [ ] Threaded server variant vs asyncio at 10, 100, 1,000 connections
- **Done when:** a short write-up with a chart exists.

**5.6 CI**
- [ ] GitHub Actions: lint, type check, tests
- **Done when:** a push shows a green check.

**5.7 README**
- [ ] Architecture diagram, benchmark chart, how to run, design decisions and limitations
- **Done when:** a stranger can clone, run, and see the numbers.

### Phase 6 — DevOps and deployment (after Phase 5, fully optional)

Today the project only runs locally. This phase deploys it. Order matters: each step builds on the last, and you can stop after any of them. **Watch AWS costs:** set a billing alarm first (6.3), use the free tier, and tear everything down when you finish a session.

**6.1 Production-ready images**
- Learn first: multi-stage Docker builds, environment variables, why secrets stay out of images.
- [ ] Small multi-stage Dockerfiles for the API and MiniRedis, running as a non-root user
- [ ] Configuration through environment variables; `.env.example` committed, real secrets never
- [ ] Health checks and graceful shutdown (MiniRedis flushes the AOF on SIGTERM)
- **Done when:** `docker compose up` works from freshly built images with no local Python installed.

**6.2 CI builds and publishes images**
- [ ] GitHub Actions builds the images and pushes them to a registry (GitHub Container Registry or AWS ECR)
- [ ] Tag images with the git commit SHA
- **Done when:** a push to `main` produces a tagged image you can pull.

**6.3 AWS account safety**
- Learn first: IAM users/roles, least privilege, why you never use the root account.
- [ ] Create the account, enable MFA, create an IAM user, set a billing alarm (e.g. $5)
- **Done when:** the billing alarm exists and the root account is not used for daily work.

**6.4 First deployment: a single server**
- Learn first: EC2, security groups, SSH, what a VPC is.
- [ ] Launch a small EC2 instance, install Docker, run the compose stack
- [ ] Open only the ports you need; Postgres and MiniRedis are not public
- **Done when:** a short link works from your phone on mobile data.

**6.5 Managed services**
- Learn first: why teams use managed databases instead of running Postgres themselves.
- [ ] Move Postgres to RDS; keep MiniRedis on the instance (or its own container) with a persistent volume for the AOF
- **Done when:** the API talks to RDS and data survives replacing the EC2 instance.

**6.6 Domain and HTTPS**
- [ ] Domain name, DNS (Route 53 or any registrar), TLS certificate, reverse proxy (Caddy or nginx)
- **Done when:** `https://yourdomain/abc12` redirects correctly.

**6.7 Infrastructure as code**
- Learn first: Terraform basics (providers, resources, state).
- [ ] Describe the EC2, security groups and RDS in Terraform
- **Done when:** `terraform apply` builds the stack and `terraform destroy` removes it completely.

**6.8 Automatic deploys**
- [ ] CI deploys on merge to `main` (SSH or SSM to pull the new image and restart); roll back by redeploying the previous SHA
- **Done when:** merging a small change updates the live site without manual steps.

**6.9 Monitoring and logs**
- [ ] Structured JSON logs; ship to CloudWatch; uptime check; alert when `/health` fails
- [ ] Optional: Prometheus + Grafana scraping `/metrics` and MiniRedis `INFO`
- **Done when:** you get an alert when you stop the API on purpose.

**6.10 Kubernetes, locally first**
- Learn first: pods, deployments, services, config maps, secrets, StatefulSets, persistent volumes.
- [ ] Run a local cluster (kind or minikube)
- [ ] Manifests (or a Helm chart): API as a Deployment with several replicas, Postgres and MiniRedis as StatefulSets with persistent volumes, an Ingress
- [ ] Liveness and readiness probes
- **Done when:** the whole system runs in the local cluster and survives killing a pod.

**6.11 Kubernetes experiments (your resume material)**
- [ ] Scale the API to 1, 2, 4 replicas and re-run the Phase 5 load test
- [ ] Kill the MiniRedis pod; show that the AOF persistent volume restores the data and the API degrades gracefully
- [ ] Horizontal Pod Autoscaler driven by CPU
- **Done when:** a short write-up with numbers exists.

**6.12 (Optional) Kubernetes on AWS**
- [ ] Deploy the same manifests to EKS, or use ECS Fargate as a simpler alternative
- **Done when:** the live domain is served from the cluster. *(EKS costs money every hour it runs, so do this in a short session and destroy it afterwards.)*

Honest note: Kubernetes is overkill for three small services, and a single EC2 instance is the right production choice here. The value of 6.10–6.12 is learning and showing that you can operate it, so say so in the README.

### Stretch goals
- Replication (primary/replica) for MiniRedis
- Snapshots (RDB-style) in addition to AOF
- Analytics dashboard endpoint
- Link expiry and custom aliases
- Later project: replace storage with a Raft-replicated log

## 6. Benchmark dataset

No external dataset is required. Generate it with `bench/seed.py`:

- **Links:** 100,000 rows (also try 1M for a scaling run). Target URLs are synthetic (`https://example.com/page/<n>`) or sampled from a public top-sites list such as Tranco for realism. The redirect path never fetches the target, so the URLs need not be live.
- **Users:** ~100 synthetic users owning the links.
- **Access pattern:** `bench/workload.py` picks codes with a **Zipf distribution**, so a small number of links get most of the traffic. This is what makes the cache hit ratio realistic. Also run a uniform-random pattern as a worst case.
- **Read/write mix:** ~95% redirects, ~5% link creation.
- **Reproducibility:** fixed random seed, dataset regenerated by one command, and the same dump loaded into Postgres before every run.
- **Cache warm-up:** a fixed warm-up period before measuring; also report a cold-start run separately.

## 7. Evaluation metrics

**Performance (headline)**

| Metric | Meaning |
|---|---|
| Latency p50 / p95 / p99 | Typical and tail redirect time; p99 is the resume number |
| Throughput (req/s) | Sustained load the system handles |
| Error rate | Failed or timed-out requests under load |
| Max concurrent clients | Where latency starts to blow up |

**Cache**

| Metric | Meaning |
|---|---|
| Cache hit ratio | Share of redirects served without touching Postgres |
| Raw ops/sec and latency | MiniRedis vs real Redis via `redis-benchmark` or a custom script |
| Memory per key | Cost of the storage design |

**Correctness and reliability**

| Metric | Meaning |
|---|---|
| Test pass rate, coverage | Target 80%+ on core code |
| AOF recovery time | Replay time for e.g. 100k keys |
| Data loss after crash | Keys lost per fsync policy (`always` vs `everysec`) |
| Rate limiter accuracy | Allows exactly N requests per window under concurrent load |
| Click-event loss | Events written to Postgres vs events sent (target 0 with the queue) |

**Concurrency experiment (asyncio vs threaded)**
- Throughput and p99 at 10, 100, 1,000, 5,000 connections; CPU and memory at each level.

**Fair-benchmark rules**
- Same machine, same dataset, same warm-up, all three setups (no cache / real Redis / MiniRedis) at the same load.
- Run each test 3+ times and report the median.
- Record hardware, Docker resource limits, and versions in the README.

## 8. Scope

**In scope:** Phases 0–5 (the core project), and Phase 6 (deployment) as an optional extension.
**Out of scope:** full Redis compatibility, clustering, Lua scripting, a full frontend app (a basic single page at `/` is included), production security hardening beyond URL validation and auth basics, multi-region or high-availability setups.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Scope creep in MiniRedis | Fixed command list; stretch goals only after Phase 5 |
| Concurrency bugs (races, blocking the loop) | Single event loop, no blocking calls, tests with many concurrent clients |
| Persistence corruption | Fsync policy, replay tests, truncated-file test |
| Stalling on a hard part | Phase 1 delivers a working backend regardless; MiniRedis is additive; Pub/Sub and compaction can be cut |
| `redis-py` handshake incompatibility | Handshake stubs in Phase 2; test against `redis-py` early |
| Unfair or noisy benchmarks | Fair-benchmark rules in section 7; benchmark inside Docker |
| Windows-specific issues | Use Docker/WSL2 for anything Linux-only |

## 10. Definition of done

- All tests pass in CI.
- `docker compose up` starts the full system.
- README includes architecture, benchmark results, and known limitations.
- Killing MiniRedis under load does not take the API down.
- You can explain every component in an interview, including the tradeoffs.

## 11. How this would scale (design discussion, not built)

The project runs on one machine. This section is for the README and for interviews: where it would break at 100x scale and what you would add. None of this is built.

| Bottleneck | What to add | Tradeoff |
|---|---|---|
| One API process | Several API instances behind a **load balancer** (nginx, AWS ALB, Kubernetes Service). The API is stateless, so any instance can serve any request. | Needs shared state (DB, cache) and health checks. |
| Postgres reads | **Read replicas** for redirect lookups; writes still go to the primary. | Replication lag: a new link may briefly 404 on a replica. |
| Postgres size and writes | **Sharding** by short code (hash of the code picks the shard). | Cross-shard queries (e.g. all links of a user) get harder. |
| Cache capacity and availability | A **cache cluster** with consistent hashing, plus replicas. | Rebalancing keys and failover are complex (this is what Redis Cluster does). |
| Global latency | A **CDN** or edge cache for popular redirects. | Slow invalidation when a link is edited or deleted. |
| Code generation across many servers | A **key-generation service** that hands out unique ID ranges to each server, then base62-encodes them. Random codes need collision checks, which get costly as the table fills. | An extra service to run; ranges are lost if a server dies (fine, codes are cheap). |
| Click analytics volume | A real **message queue** (Kafka, SQS) and batch writes into an analytics store. | More infrastructure; the current Redis-style list loses events if the cache dies. |
| Abuse | Rate limiting at the edge (WAF/CDN), plus per-user quotas. | False positives on shared IPs. |

Questions to be ready for: How do you avoid code collisions? What happens when the cache dies? How do you keep the cache consistent after an edit? Read-heavy or write-heavy, and how does that change the design?

## 12. Resume bullet (fill in real numbers)

> Built a URL shortener backend (FastAPI, PostgreSQL) with a custom Redis-compatible cache server written from scratch in Python (RESP protocol, TTL expiry, AOF persistence, pub/sub); cut p99 redirect latency by **X%** at **N** concurrent clients.

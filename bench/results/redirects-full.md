**1 client(s)**

| Setup | req/s | p50 ms | p95 ms | p99 ms | hit ratio | errors |
|---|---:|---:|---:|---:|---:|---:|
| Real Redis, rate limit and clicks on | 185 | 4.58 | 9.47 | 16.90 | 77.3% | 0 |
| MiniRedis, rate limit and clicks on | 170 | 5.05 | 10.52 | 20.94 | 77.6% | 0 |

**32 client(s)**

| Setup | req/s | p50 ms | p95 ms | p99 ms | hit ratio | errors |
|---|---:|---:|---:|---:|---:|---:|
| Real Redis, rate limit and clicks on | 180 | 158.30 | 308.11 | 378.48 | 75.5% | 0 |
| MiniRedis, rate limit and clicks on | 172 | 181.02 | 308.03 | 387.01 | 75.5% | 0 |

**Cold start** (cache empty, no warm-up, first seconds under load)

| Setup | req/s | p50 ms | p99 ms | hit ratio |
|---|---:|---:|---:|---:|
| Real Redis, rate limit and clicks on | 124 | 236.55 | 502.13 | 43.8% |
| MiniRedis, rate limit and clicks on | 124 | 238.88 | 503.36 | 44.0% |

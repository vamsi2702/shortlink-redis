**1 client(s)**

| Setup | req/s | p50 ms | p95 ms | p99 ms | hit ratio | errors |
|---|---:|---:|---:|---:|---:|---:|
| No cache (every redirect asks Postgres) | 179 | 5.06 | 9.32 | 12.35 | 0.0% | 0 |
| In-process dict (the original cache) | 200 | 4.34 | 9.81 | 13.44 | 70.4% | 0 |
| Real Redis 7 | 220 | 3.86 | 8.68 | 11.80 | 77.9% | 0 |
| MiniRedis | 216 | 3.96 | 8.62 | 12.75 | 77.9% | 0 |

**32 client(s)**

| Setup | req/s | p50 ms | p95 ms | p99 ms | hit ratio | errors |
|---|---:|---:|---:|---:|---:|---:|
| No cache (every redirect asks Postgres) | 169 | 182.18 | 276.35 | 336.46 | 0.0% | 0 |
| In-process dict (the original cache) | 179 | 161.80 | 284.36 | 350.58 | 69.2% | 0 |
| Real Redis 7 | 203 | 147.93 | 238.49 | 309.99 | 75.2% | 0 |
| MiniRedis | 188 | 156.94 | 264.27 | 334.00 | 75.5% | 0 |

**Cold start** (cache empty, no warm-up, first seconds under load)

| Setup | req/s | p50 ms | p99 ms | hit ratio |
|---|---:|---:|---:|---:|
| No cache (every redirect asks Postgres) | 161 | 189.90 | 380.98 | 0.0% |
| In-process dict (the original cache) | 145 | 207.52 | 395.60 | 45.9% |
| Real Redis 7 | 127 | 234.93 | 473.98 | 44.2% |
| MiniRedis | 163 | 184.26 | 356.95 | 46.6% |

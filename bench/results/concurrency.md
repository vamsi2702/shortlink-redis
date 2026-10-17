| Server | Connections | GET req/s | GET p50 ms | GET p99 ms | SET req/s | CPU s per 100k commands | Memory added MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| Real Redis 7 (C, event loop) | 10 | 80,841 | 0.047 | 0.335 | 80,257 | 1.31 | 0 |
| Real Redis 7 (C, event loop) | 100 | 91,996 | 0.335 | 2.759 | 85,911 | 1.22 | 1 |
| Real Redis 7 (C, event loop) | 1000 | 60,496 | 4.839 | 51.135 | 60,716 | 1.99 | 12 |
| MiniRedis, asyncio (one thread) | 10 | 20,691 | 0.447 | 1.095 | 18,829 | 4.79 | -0 |
| MiniRedis, asyncio (one thread) | 100 | 21,815 | 4.183 | 10.743 | 18,939 | 4.76 | 0 |
| MiniRedis, asyncio (one thread) | 1000 | 15,632 | 58.943 | 163.583 | 16,952 | 6.37 | 6 |
| MiniRedis, thread per connection | 10 | 9,542 | 0.823 | 24.495 | 7,623 | 13.45 | 1 |
| MiniRedis, thread per connection | 100 | 7,046 | 9.087 | 64.383 | 6,714 | 16.02 | 15 |
| MiniRedis, thread per connection | 1000 | 6,293 | 168.191 | 626.175 | 5,446 | 19.93 | 25 |

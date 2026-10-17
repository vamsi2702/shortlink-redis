import os

# Public address of the service; short links are returned as BASE_URL/<code>
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

# The default secret is for local development ONLY. Set JWT_SECRET in any real deployment.
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-secret-change-me-in-production-0123456789")
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "60"))
# bcrypt cost: each +1 doubles the hashing time. Tests lower it to stay fast.
BCRYPT_ROUNDS = int(os.environ.get("BCRYPT_ROUNDS", "12"))

# How long a redirect stays cached, and how long a "this code does not exist" answer stays cached
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "300"))
NOT_FOUND_TTL_SECONDS = int(os.environ.get("NOT_FOUND_TTL_SECONDS", "30"))

# Where MiniRedis listens, e.g. redis://localhost:6380. Empty means "do not use MiniRedis": the API
# then keeps its cache inside its own process (fine for development and for the unit tests).
MINIREDIS_URL = os.environ.get("MINIREDIS_URL", "")
# A call to MiniRedis that takes longer than this counts as a failure (the cache must be quick)
MINIREDIS_TIMEOUT_SECONDS = float(os.environ.get("MINIREDIS_TIMEOUT_SECONDS", "0.25"))
# After a failure, stop trying MiniRedis for this long, so a dead server does not slow every request
MINIREDIS_RETRY_SECONDS = float(os.environ.get("MINIREDIS_RETRY_SECONDS", "5"))

# Rate limiting: at most this many requests per client in each window (separately for redirects,
# creating links, and signing in or registering)
RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "100"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))

# Clicks: each redirect pushes an event onto this MiniRedis list; the worker drains it in batches
CLICK_QUEUE_KEY = os.environ.get("CLICK_QUEUE_KEY", "clicks")
CLICK_BATCH_SIZE = int(os.environ.get("CLICK_BATCH_SIZE", "100"))
CLICK_WORKER_INTERVAL_SECONDS = float(os.environ.get("CLICK_WORKER_INTERVAL_SECONDS", "1"))

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://shortlink:shortlink@localhost:5432/shortlink",
)

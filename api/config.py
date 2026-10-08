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

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://shortlink:shortlink@localhost:5432/shortlink",
)

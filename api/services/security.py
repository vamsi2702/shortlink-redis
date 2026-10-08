from datetime import UTC, datetime, timedelta
from functools import cache

import bcrypt
import jwt

from api import config

JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=config.BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode(), salt).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:  # malformed hash
        return False


@cache
def dummy_hash() -> str:
    """A throwaway hash, checked when the email is unknown so login takes equally long."""
    return hash_password("not-a-real-password")


def create_access_token(user_id: int, expires_in: timedelta | None = None) -> str:
    now = datetime.now(UTC)
    if expires_in is None:
        expires_in = timedelta(minutes=config.JWT_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "iat": now, "exp": now + expires_in}
    return jwt.encode(payload, config.JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> int | None:
    """Return the user id inside a valid, unexpired token, or None for anything else."""
    try:
        payload = jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
        return int(payload["sub"])
    except (jwt.PyJWTError, ValueError):
        return None

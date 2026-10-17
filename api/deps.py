from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, params
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from api import config
from api.db import SessionLocal
from api.models import User
from api.services.cache import Cache, default_cache
from api.services.clicks import ClickQueue, NullClickQueue, RedisClickQueue
from api.services.ratelimit import allow_request, client_identity
from api.services.redis_cache import Guard, RedisCache, connect
from api.services.security import decode_access_token


def get_db() -> Iterator[Session]:
    """Give each request its own database session and close it when the request ends."""
    with SessionLocal() as db:
        yield db


# Use as a route parameter type: `def route(db: DbSession)`
DbSession = Annotated[Session, Depends(get_db)]


@lru_cache
def _miniredis(url: str) -> tuple[RedisCache, RedisClickQueue]:
    """The cache and the click queue, built once. They share one connection pool and one circuit
    breaker, so when MiniRedis is down both find out from the same failure."""
    client, guard = connect(url), Guard()
    return RedisCache(client, guard), RedisClickQueue(client, guard)


def get_cache() -> Cache:
    if config.MINIREDIS_URL:
        return _miniredis(config.MINIREDIS_URL)[0]
    return default_cache


def get_click_queue() -> ClickQueue:
    if config.MINIREDIS_URL:
        return _miniredis(config.MINIREDIS_URL)[1]
    return NullClickQueue()


CacheDep = Annotated[Cache, Depends(get_cache)]
ClickQueueDep = Annotated[ClickQueue, Depends(get_click_queue)]


def rate_limited(scope: str) -> params.Depends:
    """A dependency that answers 429 when the client sends too many requests to routes of `scope`.

    Use it in a route decorator: `@router.get("/x", dependencies=[rate_limited("x")])`.
    """

    def check(request: Request, cache: CacheDep) -> None:
        if not allow_request(cache, scope, client_identity(request)):
            raise HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={"Retry-After": str(config.RATE_LIMIT_WINDOW_SECONDS)},
            )

    return Depends(check)


# auto_error=False so we can answer a missing token ourselves (401, same as a bad token)
bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DbSession,
) -> User | None:
    """No token -> None (anonymous). A token that is present but invalid -> 401."""
    if credentials is None:
        return None
    user_id = decode_access_token(credentials.credentials)
    user = db.get(User, user_id) if user_id is not None else None
    if user is None:
        raise _unauthorized("Invalid or expired token")
    return user


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def get_current_user(user: OptionalUser) -> User:
    if user is None:
        raise _unauthorized("Not authenticated")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]

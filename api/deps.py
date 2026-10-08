from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from api.db import SessionLocal
from api.models import User
from api.services.cache import Cache, default_cache
from api.services.security import decode_access_token


def get_db() -> Iterator[Session]:
    """Give each request its own database session and close it when the request ends."""
    with SessionLocal() as db:
        yield db


# Use as a route parameter type: `def route(db: DbSession)`
DbSession = Annotated[Session, Depends(get_db)]


def get_cache() -> Cache:
    return default_cache


CacheDep = Annotated[Cache, Depends(get_cache)]

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

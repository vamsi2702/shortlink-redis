from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api import config
from api.models import Click, Link
from api.services.cache import Cache
from api.services.codes import generate_code

MAX_ATTEMPTS = 5


def get_link_by_code(db: Session, code: str) -> Link | None:
    return db.scalar(select(Link).where(Link.code == code))


def link_cache_key(code: str) -> str:
    return f"link:{code}"


def resolve_long_url(db: Session, cache: Cache, code: str) -> str | None:
    """The redirect target for a code: from the cache if possible, otherwise from Postgres."""
    key = link_cache_key(code)
    cached = cache.get(key)
    if cached is not None:
        return cached or None  # an empty string means "known not to exist"
    link = get_link_by_code(db, code)
    if link is None:
        cache.set(key, "", ttl=config.NOT_FOUND_TTL_SECONDS)
        return None
    cache.set(key, link.long_url, ttl=config.CACHE_TTL_SECONDS)
    return link.long_url


def create_link(db: Session, long_url: str, user_id: int | None = None) -> Link:
    """Save a link under a fresh random code, retrying if the code is already taken."""
    for _ in range(MAX_ATTEMPTS):
        link = Link(code=generate_code(), long_url=long_url, user_id=user_id)
        try:
            # savepoint: if the unique constraint on `code` fails, only this attempt is undone
            with db.begin_nested():
                db.add(link)
        except IntegrityError:
            continue
        db.commit()
        return link
    raise RuntimeError("could not generate a unique code")


def list_user_links(db: Session, user_id: int, limit: int, offset: int) -> list[Link]:
    stmt = (
        select(Link)
        .where(Link.user_id == user_id)
        .order_by(Link.created_at.desc(), Link.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt))


def get_owned_link(db: Session, user_id: int, code: str) -> Link | None:
    """The link with this code, but only if it belongs to this user."""
    return db.scalar(select(Link).where(Link.code == code, Link.user_id == user_id))


def update_link_url(db: Session, link: Link, long_url: str) -> Link:
    link.long_url = long_url
    db.commit()
    return link


def delete_link(db: Session, link: Link) -> None:
    # clicks reference the link, so they must go first
    db.execute(delete(Click).where(Click.link_id == link.id))
    db.delete(link)
    db.commit()

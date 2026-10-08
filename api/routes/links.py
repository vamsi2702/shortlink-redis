from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from api.config import BASE_URL
from api.deps import CacheDep, CurrentUser, DbSession, OptionalUser
from api.models import Link
from api.schemas import LinkOut, ShortenRequest, UpdateLinkRequest
from api.services import links as links_service

router = APIRouter()


def to_link_out(link: Link) -> LinkOut:
    return LinkOut(
        code=link.code,
        short_url=f"{BASE_URL}/{link.code}",
        long_url=link.long_url,
        created_at=link.created_at,
    )


def get_owned_link_or_404(db: DbSession, user: CurrentUser, code: str) -> Link:
    # 404 (not 403) for other people's links, so the response doesn't reveal that the code exists
    link = links_service.get_owned_link(db, user.id, code)
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    return link


@router.post("/shorten", response_model=LinkOut, status_code=201)
def shorten(
    body: ShortenRequest, db: DbSession, user: OptionalUser, cache: CacheDep
) -> LinkOut:
    """Anyone can shorten a link. If a valid token is sent, the link belongs to that user."""
    link = links_service.create_link(db, body.url, user_id=user.id if user else None)
    # drop any "not found" answer cached for this code before it existed
    cache.delete(links_service.link_cache_key(link.code))
    return to_link_out(link)


@router.get("/links", response_model=list[LinkOut])
def list_my_links(
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[LinkOut]:
    return [to_link_out(link) for link in links_service.list_user_links(db, user.id, limit, offset)]


@router.patch("/links/{code}", response_model=LinkOut)
def update_my_link(
    code: str, body: UpdateLinkRequest, db: DbSession, user: CurrentUser, cache: CacheDep
) -> LinkOut:
    link = get_owned_link_or_404(db, user, code)
    updated = links_service.update_link_url(db, link, body.url)
    cache.delete(links_service.link_cache_key(code))
    return to_link_out(updated)


@router.delete("/links/{code}", status_code=204)
def delete_my_link(code: str, db: DbSession, user: CurrentUser, cache: CacheDep) -> Response:
    link = get_owned_link_or_404(db, user, code)
    links_service.delete_link(db, link)
    cache.delete(links_service.link_cache_key(code))
    return Response(status_code=204)

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from api.deps import CacheDep, ClickQueueDep, DbSession, rate_limited
from api.services.clicks import record_click
from api.services.links import resolve_long_url

router = APIRouter()


@router.get("/{code}", dependencies=[rate_limited("redirect")])
def redirect(code: str, db: DbSession, cache: CacheDep, queue: ClickQueueDep) -> RedirectResponse:
    long_url = resolve_long_url(db, cache, code)
    if long_url is None:
        raise HTTPException(status_code=404, detail="Short link not found")
    # Leave a note for the worker, which saves clicks to Postgres later; this is one quick push
    # and never fails the redirect
    record_click(queue, code)
    # 302 (temporary), not 301: browsers must ask us every time, so we can count clicks later
    return RedirectResponse(long_url, status_code=302)

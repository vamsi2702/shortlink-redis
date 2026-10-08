from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from api.deps import CacheDep, DbSession
from api.services.links import resolve_long_url

router = APIRouter()


@router.get("/{code}")
def redirect(code: str, db: DbSession, cache: CacheDep) -> RedirectResponse:
    long_url = resolve_long_url(db, cache, code)
    if long_url is None:
        raise HTTPException(status_code=404, detail="Short link not found")
    # 302 (temporary), not 301: browsers must ask us every time, so we can count clicks later
    return RedirectResponse(long_url, status_code=302)

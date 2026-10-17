import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from api.routes import auth, frontend, links, metrics, redirect
from api.services.metrics import LATENCY, REQUESTS
from api.services.metrics import metrics as registry

app = FastAPI(title="ShortLink")


@app.middleware("http")
async def record_metrics(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Time every request and count it by route, method and status."""
    started = time.perf_counter()
    status = 500  # what is recorded if the request crashes
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # The route TEMPLATE ("/{code}"), never the real path: every short code would otherwise
        # become its own series. It is filled in by the router while the request is handled.
        route = getattr(request.scope.get("route"), "path", "unmatched")
        if route != "/metrics":  # scraping the numbers should not change them
            elapsed = time.perf_counter() - started
            registry.inc(REQUESTS, route=route, method=request.method, status=str(status))
            registry.observe(LATENCY, elapsed, route=route)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(frontend.router)
app.include_router(auth.router)
app.include_router(links.router)
app.include_router(metrics.router)
# Keep this last: "/{code}" matches any single-segment path, so it must not shadow the routes above.
app.include_router(redirect.router)

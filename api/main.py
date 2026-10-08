from fastapi import FastAPI

from api.routes import auth, links, redirect

app = FastAPI(title="ShortLink")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(links.router)
# Keep this last: "/{code}" matches any single-segment path, so it must not shadow the routes above.
app.include_router(redirect.router)

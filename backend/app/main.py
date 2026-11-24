from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import admin, ai, auth, digest, geoip, inventory, logs, review, runners, sources, tasks, updates
from app.config import get_settings
from app.framework import registry
from app.logging_setup import configure_logging

configure_logging()

app = FastAPI(title="Steward", version=get_settings().steward_version, docs_url="/api/docs",
              openapi_url="/api/openapi.json", redoc_url=None)

CSRF_HEADER = "x-steward"


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    # A custom header cannot be set cross-site without a CORS preflight, which this API never allows.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/") \
            and request.headers.get(CSRF_HEADER) != "1" and not request.headers.get("authorization"):
        return JSONResponse({"detail": "missing X-Steward header"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.on_event("startup")
def startup() -> None:
    registry.load_all()


@app.get("/api/health")
def health():
    return {"status": "ok", "version": get_settings().steward_version}


for r in (auth.router, admin.router, sources.router, geoip.router, inventory.router, runners.router, logs.router,
          tasks.router, review.router, updates.router, ai.router, digest.router):
    app.include_router(r, prefix="/api")

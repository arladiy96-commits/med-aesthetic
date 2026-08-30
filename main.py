import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from api import router as api_router, warm_service_catalog_cache
from bot import (
    bot_webhook_enabled,
    router as telegram_router,
    setup_telegram_webhook,
)
from database import database_status, init_db

BASE_DIR = Path(__file__).resolve().parent
CLIENT_DIR = BASE_DIR / "client"
ADMIN_MOBILE_DIR = BASE_DIR / "admin" / "mobile"
ADMIN_DESKTOP_DIR = BASE_DIR / "admin" / "desktop"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

app = FastAPI(
    title="MED AESTHETIC Mini App",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)

app.include_router(api_router, prefix="/api")
app.include_router(telegram_router)

# Three physically isolated frontends.
app.mount("/client", StaticFiles(directory=CLIENT_DIR, html=True), name="client")
app.mount("/admin/mobile", StaticFiles(directory=ADMIN_MOBILE_DIR, html=True), name="admin-mobile")
app.mount("/admin/desktop", StaticFiles(directory=ADMIN_DESKTOP_DIR, html=True), name="admin-desktop")

# Existing global assets stay shared because they are content, not interface code.
ASSETS_DIR = BASE_DIR / "assets"
if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

@app.on_event("startup")
def startup() -> None:
    init_db()
    # Build the catalog in RAM before accepting client traffic.
    # This removes the Neon round-trip from every Mini App opening.
    warm_service_catalog_cache()
    setup_telegram_webhook()


@app.middleware("http")
async def cache_policy(request, call_next):
    response = await call_next(request)
    path = request.url.path

    if path == "/":
        # index.html must always pick up the newest deployment.
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif path in {"/api/services", "/api/admin/services"} or path.startswith("/api/admin/") or path.startswith("/api/availability"):
        # Live admin/availability data must never be stale. Service catalog speed
        # still comes from the server RAM cache, not from browser caching.
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/assets/"):
        # Static image assets almost never change and have their own filenames.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/css/"):
        # Let the browser reuse CSS but still revalidate after a short period.
        response.headers["Cache-Control"] = "public, max-age=300, must-revalidate"

    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    db = database_status()
    return JSONResponse(
        {
            "status": "ok" if db["ok"] else "degraded",
            "service": "med-aesthetic-mini-app",
            "bot_configured": bool(BOT_TOKEN),
            "telegram_webhook_enabled": bot_webhook_enabled(),
            "database": db,
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )

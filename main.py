import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from api import router as api_router
from database import database_status, init_db

BASE_DIR = Path(__file__).resolve().parent
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

app = FastAPI(
    title="MED AESTHETIC Mini App",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(api_router, prefix="/api")

@app.on_event("startup")
def startup() -> None:
    init_db()


@app.middleware("http")
async def no_cache_for_app(request, call_next):
    response = await call_next(request)
    if request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
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

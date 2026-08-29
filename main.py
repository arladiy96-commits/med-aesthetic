import io
import os
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from api import router as api_router
from bot import (
    bot_webhook_enabled,
    router as telegram_router,
    setup_telegram_webhook,
)
from database import database_status, init_db
from service_assets import build_service_assets, service_assets_status

BASE_DIR = Path(__file__).resolve().parent
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

# Serve the modular CSS files used by index.html.
CSS_DIR = BASE_DIR / "css"
ASSETS_DIR = BASE_DIR / "assets"
app.mount("/css", StaticFiles(directory=CSS_DIR), name="css")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.on_event("startup")
def startup() -> None:
    init_db()
    build_service_assets()
    setup_telegram_webhook()


@app.middleware("http")
async def cache_policy(request, call_next):
    response = await call_next(request)
    path = request.url.path

    if path == "/":
        # index.html must always pick up the newest deployment.
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
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
            "service_images": service_assets_status(),
        }
    )


# TEMPORARY EXPORT ROUTE.
# All source images are already publicly served under /assets/services/.
# This endpoint only bundles them into one ZIP and renames them to stable names.
SERVICE_EXPORT_MAP = {1: {'thumb': 'runtime-1-34c0886e2446d3-thumb.webp', 'full': 'runtime-1-34c0886e2446d3-full.webp', 'name': 'Чистка лица механическая'}, 2: {'thumb': 'runtime-2-a68eda41795868-thumb.webp', 'full': 'runtime-2-a68eda41795868-full.webp', 'name': 'Детский прокол ушей'}, 3: {'thumb': 'runtime-3-1054a1133baaf7-thumb.webp', 'full': 'runtime-3-1054a1133baaf7-full.webp', 'name': 'Удаление кожных новообразований'}, 4: {'thumb': 'runtime-4-28407bac0ace4f-thumb.webp', 'full': 'runtime-4-28407bac0ace4f-full.webp', 'name': 'Пирсинг мочки'}, 5: {'thumb': 'runtime-5-b7cfebc2665da6-thumb.webp', 'full': 'runtime-5-b7cfebc2665da6-full.webp', 'name': 'Пирсинг ноздри (Nostril)'}, 6: {'thumb': 'runtime-6-5b3119c59dd70c-thumb.webp', 'full': 'runtime-6-5b3119c59dd70c-full.webp', 'name': 'Вертикальный лабрет'}, 7: {'thumb': 'runtime-7-e6699a4f352518-thumb.webp', 'full': 'runtime-7-e6699a4f352518-full.webp', 'name': 'Пирсинг языка'}, 8: {'thumb': 'runtime-8-0b86306a488224-thumb.webp', 'full': 'runtime-8-0b86306a488224-full.webp', 'name': 'Пирсинг брови'}, 11: {'thumb': 'runtime-11-6842e71fe5cb0c-thumb.webp', 'full': 'runtime-11-6842e71fe5cb0c-full.webp', 'name': 'Лазерная эпиляция'}, 12: {'thumb': 'runtime-12-f3af64e2aad798-thumb.webp', 'full': 'runtime-12-f3af64e2aad798-full.webp', 'name': 'Курс аппаратной коррекции'}, 13: {'thumb': 'runtime-13-5d873afc71b096-thumb.webp', 'full': 'runtime-13-5d873afc71b096-full.webp', 'name': 'Токовая терапия'}, 14: {'thumb': 'runtime-14-5b35c7de52a2b1-thumb.webp', 'full': 'runtime-14-5b35c7de52a2b1-full.webp', 'name': 'Подарочные сертификаты'}, 15: {'thumb': 'runtime-15-17b8cf193496f3-thumb.webp', 'full': 'runtime-15-17b8cf193496f3-full.webp', 'name': 'Пирсинг пупка'}, 16: {'thumb': 'runtime-16-b171fc733c7673-thumb.webp', 'full': 'runtime-16-b171fc733c7673-full.webp', 'name': 'Чистка лица аппаратная'}, 17: {'thumb': 'runtime-17-9dac24ddf7e037-thumb.webp', 'full': 'runtime-17-9dac24ddf7e037-full.webp', 'name': 'Пирсинг сосков'}, 18: {'thumb': 'runtime-18-4d5524bb420eb0-thumb.webp', 'full': 'runtime-18-4d5524bb420eb0-full.webp', 'name': 'Индустриал'}, 19: {'thumb': 'runtime-19-fcfd7e437033bc-thumb.webp', 'full': 'runtime-19-fcfd7e437033bc-full.webp', 'name': 'Септум'}, 20: {'thumb': 'runtime-20-c5b131833446ea-thumb.webp', 'full': 'runtime-20-c5b131833446ea-full.webp', 'name': 'Бридж'}, 21: {'thumb': 'runtime-21-bcee6f4155d525-thumb.webp', 'full': 'runtime-21-bcee6f4155d525-full.webp', 'name': 'Лабрет'}, 22: {'thumb': 'runtime-22-5da40bc37ea3dc-thumb.webp', 'full': 'runtime-22-5da40bc37ea3dc-full.webp', 'name': 'Медуза'}, 23: {'thumb': 'runtime-23-882c1cb436625e-thumb.webp', 'full': 'runtime-23-882c1cb436625e-full.webp', 'name': 'Монро'}, 24: {'thumb': 'runtime-24-3b34850b7d45d4-thumb.webp', 'full': 'runtime-24-3b34850b7d45d4-full.webp', 'name': 'Трагус'}}


@app.get("/_med-service-images-export", include_in_schema=False)
async def export_service_images():
    service_dir = ASSETS_DIR / "services"
    buffer = io.BytesIO()

    missing = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for service_id, files in SERVICE_EXPORT_MAP.items():
            for kind in ("thumb", "full"):
                source = service_dir / files[kind]
                if not source.is_file():
                    missing.append(source.name)
                    continue

                archive.write(
                    source,
                    arcname=f"assets/services/service-{service_id}-{kind}.webp",
                )

        # Small manifest makes the archive self-describing.
        manifest_lines = ["MED AESTHETIC service photos", ""]
        for service_id, files in SERVICE_EXPORT_MAP.items():
            manifest_lines.append(
                f"{service_id}: {files['name']} -> "
                f"service-{service_id}-thumb.webp / service-{service_id}-full.webp"
            )
        if missing:
            manifest_lines += ["", "MISSING:"] + missing

        archive.writestr(
            "assets/services/README.txt",
            "\n".join(manifest_lines),
        )

    buffer.seek(0)
    headers = {
        "Content-Disposition": 'attachment; filename="med-aesthetic-service-images.zip"',
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers=headers,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )

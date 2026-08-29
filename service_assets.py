from __future__ import annotations

import base64
import hashlib
import io
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import RLock

from PIL import Image, ImageOps

from database import db


BASE_DIR = Path(__file__).resolve().parent
SERVICE_DIR = BASE_DIR / "assets" / "services"
SERVICE_DIR.mkdir(parents=True, exist_ok=True)

MAX_REMOTE_BYTES = 20 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 60_000_000

_manifest: dict[int, dict[str, str]] = {}
_manifest_lock = RLock()


def _content_hash(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:14]


def _file_hash(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:14]


def _static_override(service_id: int) -> dict[str, str] | None:
    """Allow permanent GitHub files later, without changing Python.

    Put:
      assets/services/service-<ID>-thumb.webp
      assets/services/service-<ID>-full.webp

    A content hash is added to the URL so a replaced file can never be
    confused with the browser's old cached version.
    """
    thumb = SERVICE_DIR / f"service-{service_id}-thumb.webp"
    full = SERVICE_DIR / f"service-{service_id}-full.webp"

    if not thumb.exists() and not full.exists():
        return None

    if not thumb.exists():
        thumb = full
    if not full.exists():
        full = thumb

    return {
        "thumb": f"/assets/services/{thumb.name}?v={_file_hash(thumb)}",
        "full": f"/assets/services/{full.name}?v={_file_hash(full)}",
    }


def _read_source(value: str) -> bytes:
    value = str(value or "").strip()
    if not value:
        raise ValueError("empty image")

    if value.startswith("data:image/") and ";base64," in value:
        encoded = value.split(",", 1)[1]
        data = base64.b64decode(encoded, validate=False)
        if len(data) > MAX_REMOTE_BYTES:
            raise ValueError("image too large")
        return data

    if value.startswith(("http://", "https://")):
        req = urllib.request.Request(
            value,
            headers={
                "User-Agent": "Mozilla/5.0 MED-AESTHETIC/1.0",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = response.read(MAX_REMOTE_BYTES + 1)
        if len(data) > MAX_REMOTE_BYTES:
            raise ValueError("image too large")
        return data

    # Existing local app asset.
    if value.startswith("/assets/"):
        local = BASE_DIR / value.lstrip("/")
        if local.is_file():
            return local.read_bytes()

    raise ValueError("unsupported image source")


def _open_image(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    image = ImageOps.exif_transpose(image)

    if image.mode == "RGBA":
        bg = Image.new("RGB", image.size, (9, 7, 13))
        bg.paste(image, mask=image.getchannel("A"))
        image = bg
    else:
        image = image.convert("RGB")

    return image


def _save_webp(image: Image.Image, path: Path, max_side: int, quality: int) -> None:
    work = image.copy()
    work.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    work.save(
        path,
        format="WEBP",
        quality=quality,
        method=6,
        optimize=True,
    )


def _build_one(service_id: int, image_url: str | None) -> tuple[int, dict[str, str] | None]:
    permanent = _static_override(service_id)
    if permanent:
        return service_id, permanent

    if not image_url:
        return service_id, None

    try:
        source = _read_source(str(image_url))
        digest = _content_hash(source)

        thumb_name = f"runtime-{service_id}-{digest}-thumb.webp"
        full_name = f"runtime-{service_id}-{digest}-full.webp"

        thumb_path = SERVICE_DIR / thumb_name
        full_path = SERVICE_DIR / full_name

        if not thumb_path.exists() or not full_path.exists():
            image = _open_image(source)
            if not thumb_path.exists():
                _save_webp(image, thumb_path, max_side=720, quality=76)
            if not full_path.exists():
                _save_webp(image, full_path, max_side=1600, quality=82)

        # Remove older runtime variants for this service.
        for old in SERVICE_DIR.glob(f"runtime-{service_id}-*.webp"):
            if old.name not in {thumb_name, full_name}:
                try:
                    old.unlink()
                except OSError:
                    pass

        return service_id, {
            "thumb": f"/assets/services/{thumb_name}",
            "full": f"/assets/services/{full_name}",
        }
    except Exception as exc:
        print(f"[service-assets] service {service_id}: {exc}")
        return service_id, None


def build_service_assets() -> dict:
    """Build a small local image cache before the app starts accepting traffic."""
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, image_url
            FROM beauty_services
            WHERE deleted_at IS NULL
            ORDER BY id
            """
        ).fetchall()

    result: dict[int, dict[str, str]] = {}

    # Remote legacy images are fetched concurrently so deploy startup stays quick.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="service-image") as pool:
        futures = [
            pool.submit(
                _build_one,
                int(row["id"]),
                row.get("image_url"),
            )
            for row in rows
        ]

        for future in as_completed(futures):
            service_id, urls = future.result()
            if urls:
                result[service_id] = urls

    with _manifest_lock:
        _manifest.clear()
        _manifest.update(result)

    print(
        f"[service-assets] ready: {len(result)}/{len(rows)} service image(s)"
    )
    return {
        "ok": True,
        "prepared": len(result),
        "total": len(rows),
    }


def service_asset_urls(
    service_id: int,
    image_url: str | None = None,
    version: int = 0,
) -> dict[str, str]:
    """Return light card/detail URLs without exposing Base64 in JSON."""
    service_id = int(service_id)

    with _manifest_lock:
        ready = _manifest.get(service_id)

    if ready:
        return dict(ready)

    permanent = _static_override(service_id)
    if permanent:
        return permanent

    legacy = str(image_url or "").strip()
    if not legacy:
        return {"thumb": "", "full": ""}

    if legacy.startswith("data:image/"):
        # Emergency fallback if an individual source could not be converted.
        url = f"/api/services/{service_id}/image?v={int(version or 0)}"
        return {"thumb": url, "full": url}

    return {"thumb": legacy, "full": legacy}


def service_assets_status() -> dict:
    with _manifest_lock:
        count = len(_manifest)
    return {
        "directory": str(SERVICE_DIR),
        "prepared": count,
    }

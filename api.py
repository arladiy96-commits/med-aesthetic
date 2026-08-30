from __future__ import annotations

import json
from datetime import date, time, timedelta
from functools import lru_cache
from threading import RLock
from time import monotonic
from pathlib import Path
import hashlib
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation

from auth import validate_telegram_init_data
from bot import make_admin_invite_link, safe_set_role_menu_button, send_notification, send_notifications
from database import (
    create_admin_invite,
    db,
    grant_admin,
    list_admins,
    resolve_effective_role,
    revoke_admin,
    upsert_app_user,
)

router = APIRouter()


class BookingCreate(BaseModel):
    service_id: int = Field(gt=0)
    service_name: str = Field(min_length=1, max_length=180)
    client_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=5, max_length=32)
    booking_date: date
    booking_time: time


class BookingStatusUpdate(BaseModel):
    status: Literal["pending", "confirmed", "completed", "cancelled", "no_show"]


class AdminBookingUpdate(BaseModel):
    status: Optional[Literal["pending", "confirmed", "completed", "cancelled", "no_show"]] = None
    booking_date: Optional[date] = None
    booking_time: Optional[time] = None
    note: Optional[str] = Field(default=None, max_length=2000)


class AdminBookingCreate(BaseModel):
    client_name: str = Field(min_length=2, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=32)
    telegram_user_id: Optional[int] = Field(default=None, gt=0)
    service_id: int = Field(gt=0)
    booking_date: date
    booking_time: time
    note: Optional[str] = Field(default=None, max_length=2000)


class BlockedSlotCreate(BaseModel):
    booking_date: date
    booking_time: time
    label: Optional[str] = Field(default=None, max_length=120)


class ClientNoteUpdate(BaseModel):
    note: str = Field(default="", max_length=3000)


class AdminGrant(BaseModel):
    telegram_user_id: int = Field(gt=0)


class ServiceCreate(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=180)
    price: Optional[int] = Field(default=None, ge=0)
    duration: Optional[int] = Field(default=None, ge=1, le=1440)
    description: Optional[str] = Field(default=None, max_length=5000)
    includes: list[str] = Field(default_factory=list, max_length=30)
    is_active: bool = True


class ServiceUpdate(BaseModel):
    category: Optional[str] = Field(default=None, min_length=1, max_length=100)
    name: Optional[str] = Field(default=None, min_length=1, max_length=180)
    price: Optional[int] = Field(default=None, ge=0)
    duration: Optional[int] = Field(default=None, ge=1, le=1440)
    description: Optional[str] = Field(default=None, max_length=5000)
    includes: Optional[list[str]] = Field(default=None, max_length=30)
    is_active: Optional[bool] = None


BASE_DIR = Path(__file__).resolve().parent
SERVICE_IMAGE_DIR = BASE_DIR / "assets" / "services"


@lru_cache(maxsize=256)
def _static_service_images(service_id: int) -> dict[str, str]:
    """Return permanent, cache-safe service image URLs.

    The files live in GitHub under assets/services. A short content hash is
    appended to the URL. If a file is ever replaced in a future deploy, its
    URL changes automatically, so Telegram/WebView cannot flash an old cached
    photo before the new one.
    """
    service_id = int(service_id)
    thumb = SERVICE_IMAGE_DIR / f"service-{service_id}-thumb.webp"
    full = SERVICE_IMAGE_DIR / f"service-{service_id}-full.webp"

    def versioned(path: Path) -> str:
        if not path.is_file():
            return ""
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
        return f"/assets/services/{path.name}?v={digest}"

    thumb_url = versioned(thumb)
    full_url = versioned(full)

    # New services added from the admin panel intentionally have no editable
    # photo. Until a permanent WebP is added to GitHub, use the existing
    # lightweight beauty banner instead of a broken image URL.
    fallback = "/assets/hero.webp"
    return {
        "thumb": thumb_url or full_url or fallback,
        "full": full_url or thumb_url or fallback,
    }


def _service_payload(row: dict) -> dict:
    includes = row.get("includes") or []
    if not isinstance(includes, list):
        includes = []

    images = _static_service_images(int(row["id"]))

    return {
        "id": int(row["id"]),
        "cat": row["category"],
        "name": row["name"],
        "price": row.get("price"),
        "duration": row.get("duration"),
        "img": images["thumb"],
        "fullImg": images["full"],
        "desc": row.get("description") or "",
        "includes": [str(x) for x in includes],
        "isActive": bool(row.get("is_active", True)),
    }


def _model_changes(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def _booking_payload(row: dict, include_client: bool = False) -> dict:
    result = {
        "id": str(row["id"]),
        "serviceId": row["service_id"],
        "serviceName": row["service_name"],
        "masterId": row["master_id"],
        "masterName": row["master_name"],
        "date": row["booking_date"].isoformat(),
        "time": row["booking_time"].strftime("%H:%M"),
        "status": row["status"],
        "createdAt": row["created_at"].isoformat(),
    }
    if include_client:
        first_name = row.get("first_name")
        username = row.get("username")
        client_name = row.get("client_name")
        phone = row.get("phone")
        result.update(
            {
                "telegramUserId": int(row["telegram_user_id"]) if row.get("telegram_user_id") is not None else None,
                "clientTelegramName": first_name,
                "clientUsername": username,
                "clientName": client_name or first_name or (f"@{username}" if username else "Клиент"),
                "clientPhone": phone,
                "note": row.get("note") or "",
                "bookingSource": row.get("booking_source") or "client",
                "priceSnapshot": row.get("price_snapshot"),
            }
        )
    return result


def _identity(init_data: str | None) -> tuple[dict, dict]:
    telegram_user = validate_telegram_init_data(init_data or "")
    app_user = upsert_app_user(telegram_user)
    return telegram_user, app_user


def _require_creator(app_user: dict) -> None:
    if app_user.get("role") != "creator":
        raise HTTPException(status_code=403, detail="Требуются права создателя")


def _require_staff(app_user: dict) -> None:
    if app_user.get("role") not in {"creator", "admin"}:
        raise HTTPException(status_code=403, detail="Требуются права администратора")



def _staff_chat_ids() -> list[int]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT telegram_user_id
            FROM app_users
            WHERE role IN ('creator', 'admin')
            ORDER BY telegram_user_id
            """
        ).fetchall()
    return [int(r["telegram_user_id"]) for r in rows]


def _date_ru(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _client_label(
    client_name: str | None,
    first_name: str | None,
    username: str | None,
    telegram_user_id: int,
) -> str:
    if client_name:
        return client_name
    if first_name and username:
        return f"{first_name} (@{username})"
    if first_name:
        return first_name
    if username:
        return f"@{username}"
    return f"Telegram ID {telegram_user_id}"


def _new_booking_admin_text(
    *,
    booking_id: int,
    telegram_user_id: int,
    first_name: str | None,
    username: str | None,
    client_name: str,
    phone: str,
    service_name: str,
    master_name: str | None,
    booking_date: date,
    booking_time: time,
) -> str:
    client = _client_label(client_name, first_name, username, telegram_user_id)
    telegram = f"@{username}" if username else f"ID {telegram_user_id}"
    return (
        "🆕 Новая заявка на запись MED AESTHETIC\n\n"
        f"Клиент: {client}\n"
        f"Телефон: {phone}\n"
        f"Telegram: {telegram}\n"
        f"Услуга: {service_name}\n"
        f"Мастер: {master_name or 'Людмила'}\n"
        f"Дата: {_date_ru(booking_date)}\n"
        f"Время: {booking_time.strftime('%H:%M')}\n"
        f"Запись #{booking_id}"
    )


def _status_client_text(row: dict) -> str:
    status = str(row["status"])
    previous_status = str(row.get("previous_status") or "")
    if status == "cancelled" and previous_status == "pending":
        title = "❌ Заявка на запись не подтверждена"
    else:
        title = {
            "pending": "⏳ Запись ожидает подтверждения",
            "confirmed": "✅ Ваша запись подтверждена",
            "completed": "✨ Визит завершён",
            "cancelled": "❌ Запись отменена",
            "no_show": "ℹ️ Визит отмечен как несостоявшийся",
        }.get(status, "ℹ️ Статус записи изменён")

    return (
        f"{title}\n\n"
        f"{row['service_name']}\n"
        f"Мастер: {row.get('master_name') or 'Людмила'}\n"
        f"{_date_ru(row['booking_date'])} в {row['booking_time'].strftime('%H:%M')}\n"
        f"Запись #{row['id']}"
    )


def _client_cancelled_admin_text(row: dict) -> str:
    client = _client_label(
        row.get("client_name"),
        row.get("first_name"),
        row.get("username"),
        int(row["telegram_user_id"]),
    )
    phone = row.get("phone") or "не указан"
    return (
        "⚠️ Клиент отменил запись\n\n"
        f"Клиент: {client}\n"
        f"Телефон: {phone}\n"
        f"Услуга: {row['service_name']}\n"
        f"Мастер: {row.get('master_name') or 'Людмила'}\n"
        f"Дата: {_date_ru(row['booking_date'])}\n"
        f"Время: {row['booking_time'].strftime('%H:%M')}\n"
        f"Запись #{row['id']}"
    )


@router.get("/me")
def me(
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
    x_act_as_role: str | None = Header(
        default=None, alias="X-Act-As-Role"
    ),
):
    telegram_user, app_user = _identity(x_telegram_init_data)
    actual_role = app_user["role"]
    effective_role = resolve_effective_role(actual_role, x_act_as_role)

    return {
        "ok": True,
        "user": {
            "telegramUserId": int(telegram_user["id"]),
            "firstName": app_user.get("first_name"),
            "lastName": app_user.get("last_name"),
            "username": app_user.get("username"),
            "actualRole": actual_role,
            "effectiveRole": effective_role,
        },
        "roleSwitch": {
            "creator": ["creator", "client"],
            "admin": ["admin"],
            "client": ["client"],
        }[actual_role],
    }


@router.post("/admin-invites")
def new_admin_invite(
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)

    invite = create_admin_invite(
        created_by=int(app_user["telegram_user_id"])
    )

    try:
        link = make_admin_invite_link(invite["token"])
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Не удалось получить ссылку Telegram: {str(exc)[:140]}",
        )

    return {
        "ok": True,
        "invite": {
            "link": link,
            "expiresAt": invite["expires_at"].isoformat(),
        },
    }


@router.post("/admins")
def add_admin(
    payload: AdminGrant,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)

    result = grant_admin(int(payload.telegram_user_id))
    if not result.get("ok"):
        if result.get("reason") == "creator":
            raise HTTPException(
                status_code=400,
                detail="Создателю уже доступны все права",
            )
        raise HTTPException(
            status_code=400,
            detail="Не удалось добавить администратора",
        )

    user = result["user"]
    telegram_user_id = int(user["telegram_user_id"])

    background_tasks.add_task(
        safe_set_role_menu_button,
        telegram_user_id,
        "admin",
    )
    background_tasks.add_task(
        send_notification,
        telegram_user_id,
        "✅ Вам выданы права администратора MED AESTHETIC.\n\n"
        "Теперь приложение будет сразу открываться в админке.",
    )

    return {
        "ok": True,
        "admin": {
            "telegramUserId": telegram_user_id,
            "firstName": user.get("first_name"),
            "lastName": user.get("last_name"),
            "username": user.get("username"),
        },
    }


@router.get("/admins")
def admins(
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)

    result = []
    for row in list_admins():
        result.append(
            {
                "telegramUserId": int(row["telegram_user_id"]),
                "firstName": row.get("first_name"),
                "lastName": row.get("last_name"),
                "username": row.get("username"),
                "lastSeenAt": (
                    row["last_seen_at"].isoformat()
                    if row.get("last_seen_at")
                    else None
                ),
            }
        )

    return {"ok": True, "admins": result}


@router.delete("/admins/{telegram_user_id}")
def remove_admin(
    telegram_user_id: int,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)

    if not revoke_admin(telegram_user_id):
        raise HTTPException(
            status_code=404,
            detail="Администратор не найден или его нельзя удалить",
        )

    background_tasks.add_task(
        safe_set_role_menu_button,
        telegram_user_id,
        "client",
    )
    background_tasks.add_task(
        send_notification,
        telegram_user_id,
        "ℹ️ Права администратора MED AESTHETIC отключены.\n\n"
        "Приложение теперь открывается в режиме покупателя.",
    )

    return {"ok": True}


# ---------------------------------------------------------------------------
# SERVICE CATALOG MEMORY CACHE
# Neon remains the source of truth, but clients never wait for a fresh DB
# connection just to render the catalog. The cache is warmed during app startup
# and refreshed immediately after every admin service mutation.
# ---------------------------------------------------------------------------
_service_cache_lock = RLock()
_service_cache_public: tuple[dict, ...] | None = None
_service_cache_admin: tuple[dict, ...] | None = None


def _read_service_rows_from_db() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, category, name, price, duration,
                   description, includes, is_active, sort_order
            FROM beauty_services
            WHERE deleted_at IS NULL
            ORDER BY sort_order ASC, id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def warm_service_catalog_cache() -> dict:
    """Load the full service catalog once and keep ready-to-send payloads in RAM."""
    rows = _read_service_rows_from_db()
    admin_payload = tuple(_service_payload(row) for row in rows)
    public_payload = tuple(
        item for row, item in zip(rows, admin_payload)
        if bool(row.get("is_active", True))
    )

    global _service_cache_public, _service_cache_admin
    with _service_cache_lock:
        _service_cache_public = public_payload
        _service_cache_admin = admin_payload

    print(
        f"[services-cache] warmed: public={len(public_payload)}, "
        f"admin={len(admin_payload)}"
    )
    return {
        "public": len(public_payload),
        "admin": len(admin_payload),
    }


def _service_catalog_cached(admin: bool = False) -> list[dict]:
    global _service_cache_public, _service_cache_admin

    with _service_cache_lock:
        cached = _service_cache_admin if admin else _service_cache_public

    # Safety fallback only. Normal first request never reaches this because
    # main.py warms the cache before the app starts accepting traffic.
    if cached is None:
        warm_service_catalog_cache()
        with _service_cache_lock:
            cached = _service_cache_admin if admin else _service_cache_public

    # Return a new list wrapper; inner payload dicts are treated as immutable.
    return list(cached or ())


@router.get("/services")
def list_services():
    return {"ok": True, "services": _service_catalog_cached(admin=False)}


@router.get("/admin/services")
def list_admin_services(
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)

    return {"ok": True, "services": _service_catalog_cached(admin=True)}


@router.post("/admin/services")
def create_admin_service(
    payload: ServiceCreate,
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)

    includes = [str(x).strip()[:300] for x in payload.includes if str(x).strip()]

    with db() as conn:
        row = conn.execute(
            """
            INSERT INTO beauty_services (
                category, name, price, duration, description,
                includes, is_active, sort_order
            )
            VALUES (
                %s,%s,%s,%s,%s,%s::jsonb,%s,
                COALESCE((SELECT MAX(sort_order) + 1 FROM beauty_services), 1)
            )
            RETURNING id, category, name, price, duration,
                      description, includes, is_active, sort_order
            """,
            (
                payload.category.strip(),
                payload.name.strip(),
                payload.price,
                payload.duration,
                (payload.description or "").strip() or None,
                json.dumps(includes, ensure_ascii=False),
                payload.is_active,
            ),
        ).fetchone()

    warm_service_catalog_cache()
    return {"ok": True, "service": _service_payload(row)}


@router.patch("/admin/services/{service_id}")
def update_admin_service(
    service_id: int,
    payload: ServiceUpdate,
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)

    changes = _model_changes(payload)
    if not changes:
        raise HTTPException(status_code=400, detail="Нет изменений")

    sets = []
    values = []

    for key, value in changes.items():
        if key == "category":
            sets.append("category=%s")
            values.append(str(value).strip())
        elif key == "name":
            sets.append("name=%s")
            values.append(str(value).strip())
        elif key == "price":
            sets.append("price=%s")
            values.append(value)
        elif key == "duration":
            sets.append("duration=%s")
            values.append(value)
        elif key == "description":
            sets.append("description=%s")
            values.append((value or "").strip() or None)
        elif key == "includes":
            cleaned = [
                str(x).strip()[:300]
                for x in (value or [])
                if str(x).strip()
            ]
            sets.append("includes=%s::jsonb")
            values.append(json.dumps(cleaned, ensure_ascii=False))
        elif key == "is_active":
            sets.append("is_active=%s")
            values.append(bool(value))

    if not sets:
        raise HTTPException(status_code=400, detail="Нет изменений")

    values.append(service_id)

    with db() as conn:
        row = conn.execute(
            f"""
            UPDATE beauty_services
            SET {", ".join(sets)}, updated_at=NOW()
            WHERE id=%s AND deleted_at IS NULL
            RETURNING id, category, name, price, duration,
                      description, includes, is_active, sort_order
            """,
            tuple(values),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Услуга не найдена")

    warm_service_catalog_cache()
    return {"ok": True, "service": _service_payload(row)}


@router.delete("/admin/services/{service_id}")
def delete_admin_service(
    service_id: int,
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)

    with db() as conn:
        row = conn.execute(
            """
            UPDATE beauty_services
            SET is_active=FALSE, deleted_at=NOW(), updated_at=NOW()
            WHERE id=%s AND deleted_at IS NULL
            RETURNING id
            """,
            (service_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Услуга не найдена")

    warm_service_catalog_cache()
    return {"ok": True}


AVAILABILITY_CACHE_TTL_SECONDS = 15.0
_availability_cache_lock = RLock()
_availability_cache: dict[tuple[int, str, int], tuple[float, dict]] = {}


def _clear_availability_cache() -> None:
    with _availability_cache_lock:
        _availability_cache.clear()


def _availability_range_payload(start_date: date, days: int, master_id: int = 1) -> dict:
    days = max(1, min(int(days), 31))
    end_date = start_date + timedelta(days=days - 1)
    cache_key = (int(master_id), start_date.isoformat(), days)
    now = monotonic()

    # Keep the lock while the cache miss is filled. This deliberately acts as
    # a single-flight guard: if 10 clients open the calendar at once, only the
    # first request reaches Neon; the others wait briefly and reuse the result.
    with _availability_cache_lock:
        cached = _availability_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        with db() as conn:
            rows = conn.execute(
                """
                SELECT booking_date, booking_time
                FROM beauty_bookings
                WHERE master_id=%s
                  AND booking_date BETWEEN %s AND %s
                  AND status IN ('pending','confirmed')
                UNION
                SELECT booking_date, booking_time
                FROM beauty_blocked_slots
                WHERE master_id=%s
                  AND booking_date BETWEEN %s AND %s
                ORDER BY booking_date, booking_time
                """,
                (master_id, start_date, end_date, master_id, start_date, end_date),
            ).fetchall()

        availability = {
            (start_date + timedelta(days=i)).isoformat(): []
            for i in range(days)
        }
        for row in rows:
            day_key = row["booking_date"].isoformat()
            if day_key in availability:
                availability[day_key].append(row["booking_time"].strftime("%H:%M"))

        payload = {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "days": days,
            "cacheTtlSeconds": int(AVAILABILITY_CACHE_TTL_SECONDS),
            "availability": availability,
        }
        _availability_cache[cache_key] = (
            monotonic() + AVAILABILITY_CACHE_TTL_SECONDS,
            payload,
        )
        return payload


def _slot_is_blocked(conn, booking_date: date, booking_time: time) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM beauty_blocked_slots
            WHERE master_id=1 AND booking_date=%s AND booking_time=%s
            LIMIT 1
            """,
            (booking_date, booking_time),
        ).fetchone()
    )


def _slot_has_active_booking(
    conn,
    booking_date: date,
    booking_time: time,
    *,
    exclude_booking_id: int | None = None,
) -> bool:
    if exclude_booking_id is None:
        row = conn.execute(
            """
            SELECT 1
            FROM beauty_bookings
            WHERE master_id=1
              AND booking_date=%s
              AND booking_time=%s
              AND status IN ('pending','confirmed')
            LIMIT 1
            """,
            (booking_date, booking_time),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1
            FROM beauty_bookings
            WHERE master_id=1
              AND booking_date=%s
              AND booking_time=%s
              AND status IN ('pending','confirmed')
              AND id<>%s
            LIMIT 1
            """,
            (booking_date, booking_time, exclude_booking_id),
        ).fetchone()
    return bool(row)


def _reschedule_client_text(row: dict) -> str:
    return (
        "MED AESTHETIC\n\n"
        "Ваша запись перенесена администратором.\n"
        f"{row['service_name']}\n"
        f"{_date_ru(row['booking_date'])} в {row['booking_time'].strftime('%H:%M')}\n"
        f"Мастер: {row.get('master_name') or 'Людмила'}"
    )


def _manual_booking_client_text(row: dict) -> str:
    return (
        "MED AESTHETIC\n\n"
        "Администратор создал для вас запись.\n"
        f"{row['service_name']}\n"
        f"{_date_ru(row['booking_date'])} в {row['booking_time'].strftime('%H:%M')}\n"
        f"Мастер: {row.get('master_name') or 'Людмила'}"
    )


@router.get("/availability-range")
def booking_availability_range(
    start_date: date,
    days: int = 14,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    # One request returns the whole visible calendar. Server RAM keeps it for
    # 15 seconds, so simultaneous clients reuse one Neon result.
    _identity(x_telegram_init_data)
    if days < 1 or days > 31:
        raise HTTPException(status_code=400, detail="days должен быть от 1 до 31")
    payload = _availability_range_payload(start_date, days, master_id=1)
    return {"ok": True, **payload}


@router.get("/availability")
def booking_availability(
    booking_date: date,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    # Kept for compatibility with older deployed clients.
    _identity(x_telegram_init_data)
    payload = _availability_range_payload(booking_date, 1, master_id=1)
    return {
        "ok": True,
        "date": booking_date.isoformat(),
        "unavailable": payload["availability"].get(booking_date.isoformat(), []),
        "cacheTtlSeconds": payload["cacheTtlSeconds"],
    }


@router.get("/bookings")
def list_bookings(
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    user, _ = _identity(x_telegram_init_data)
    uid = int(user["id"])

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, service_id, service_name, master_id, master_name,
                   booking_date, booking_time, status, created_at
            FROM beauty_bookings
            WHERE telegram_user_id = %s
            ORDER BY booking_date ASC, booking_time ASC, id ASC
            """,
            (uid,),
        ).fetchall()

    return {
        "ok": True,
        "bookings": [_booking_payload(r) for r in rows],
    }


@router.get("/admin/bookings")
def list_admin_bookings(
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, telegram_user_id, first_name, username, client_name, phone,
                   service_id, service_name, master_id, master_name,
                   booking_date, booking_time, status, created_at,
                   note, booking_source, price_snapshot
            FROM beauty_bookings
            ORDER BY booking_date ASC, booking_time ASC, id ASC
            """
        ).fetchall()

    return {
        "ok": True,
        "bookings": [_booking_payload(r, include_client=True) for r in rows],
    }


@router.post("/admin/bookings")
def create_admin_booking(
    payload: AdminBookingCreate,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    admin_user, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)

    client_name = payload.client_name.strip()
    phone = (payload.phone or "").strip() or None
    note = (payload.note or "").strip() or None
    telegram_user_id = payload.telegram_user_id
    first_name = None
    username = None

    try:
        with db() as conn:
            service = conn.execute(
                """
                SELECT id, name, price
                FROM beauty_services
                WHERE id=%s AND deleted_at IS NULL
                """,
                (payload.service_id,),
            ).fetchone()
            if not service:
                raise HTTPException(status_code=409, detail="Услуга недоступна")

            if _slot_is_blocked(conn, payload.booking_date, payload.booking_time):
                raise HTTPException(status_code=409, detail="Это время закрыто в календаре")
            if _slot_has_active_booking(conn, payload.booking_date, payload.booking_time):
                raise HTTPException(status_code=409, detail="Это время уже занято")

            if telegram_user_id:
                person = conn.execute(
                    "SELECT first_name, username FROM app_users WHERE telegram_user_id=%s",
                    (telegram_user_id,),
                ).fetchone()
                if person:
                    first_name = person.get("first_name")
                    username = person.get("username")

            row = conn.execute(
                """
                INSERT INTO beauty_bookings (
                    telegram_user_id, first_name, username, client_name, phone,
                    service_id, service_name, master_id, master_name,
                    booking_date, booking_time, status, note, booking_source, price_snapshot
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,1,'Людмила',%s,%s,'confirmed',%s,'admin',%s)
                RETURNING id, telegram_user_id, first_name, username, client_name, phone,
                          service_id, service_name, master_id, master_name,
                          booking_date, booking_time, status, created_at,
                          note, booking_source, price_snapshot
                """,
                (
                    telegram_user_id, first_name, username, client_name, phone,
                    payload.service_id, str(service["name"]),
                    payload.booking_date, payload.booking_time, note, service.get("price"),
                ),
            ).fetchone()
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="Это время уже занято")

    _clear_availability_cache()

    if row.get("telegram_user_id") is not None:
        background_tasks.add_task(
            send_notification,
            int(row["telegram_user_id"]),
            _manual_booking_client_text(row),
        )

    return {"ok": True, "booking": _booking_payload(row, include_client=True)}


@router.patch("/admin/bookings/{booking_id}")
def update_admin_booking(
    booking_id: int,
    payload: AdminBookingUpdate,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)

    changes = _model_changes(payload)
    if not changes:
        raise HTTPException(status_code=400, detail="Нет изменений")

    try:
        with db() as conn:
            current = conn.execute(
                """
                SELECT id, telegram_user_id, first_name, username, client_name, phone,
                       service_id, service_name, master_id, master_name,
                       booking_date, booking_time, status, created_at,
                       note, booking_source, price_snapshot
                FROM beauty_bookings WHERE id=%s
                """,
                (booking_id,),
            ).fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Запись не найдена")

            new_date = changes.get("booking_date", current["booking_date"])
            new_time = changes.get("booking_time", current["booking_time"])
            moved = new_date != current["booking_date"] or new_time != current["booking_time"]

            if moved and _slot_is_blocked(conn, new_date, new_time):
                raise HTTPException(status_code=409, detail="Это время закрыто в календаре")
            if moved and _slot_has_active_booking(
                conn, new_date, new_time, exclude_booking_id=booking_id
            ):
                raise HTTPException(status_code=409, detail="Это время уже занято")

            sets=[]
            values=[]
            for field, column in (
                ("status","status"),
                ("booking_date","booking_date"),
                ("booking_time","booking_time"),
                ("note","note"),
            ):
                if field in changes:
                    sets.append(f"{column}=%s")
                    value=changes[field]
                    if field=="note" and value is not None:
                        value=value.strip() or None
                    values.append(value)
            sets.append("updated_at=NOW()")
            values.append(booking_id)

            row = conn.execute(
                f"""
                UPDATE beauty_bookings
                SET {', '.join(sets)}
                WHERE id=%s
                RETURNING id, telegram_user_id, first_name, username, client_name, phone,
                          service_id, service_name, master_id, master_name,
                          booking_date, booking_time, status, created_at,
                          note, booking_source, price_snapshot
                """,
                tuple(values),
            ).fetchone()
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="Это время уже занято")

    if moved or "status" in changes:
        _clear_availability_cache()

    row = dict(row)
    row["previous_status"] = current.get("status")
    uid=row.get("telegram_user_id")
    if uid is not None:
        if moved:
            background_tasks.add_task(send_notification, int(uid), _reschedule_client_text(row))
        elif "status" in changes:
            background_tasks.add_task(send_notification, int(uid), _status_client_text(row))

    return {"ok": True, "booking": _booking_payload(row, include_client=True)}


@router.get("/admin/blocks")
def list_admin_blocks(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    start = from_date or (date.today() - timedelta(days=1))
    end = to_date or (date.today() + timedelta(days=180))
    with db() as conn:
        rows=conn.execute(
            """
            SELECT id, master_id, booking_date, booking_time, label, created_at
            FROM beauty_blocked_slots
            WHERE master_id=1 AND booking_date BETWEEN %s AND %s
            ORDER BY booking_date, booking_time
            """,
            (start,end),
        ).fetchall()
    return {"ok":True,"blocks":[{
        "id":str(r["id"]),
        "masterId":r["master_id"],
        "date":r["booking_date"].isoformat(),
        "time":r["booking_time"].strftime("%H:%M"),
        "label":r.get("label") or "Закрыто",
    } for r in rows]}


@router.post("/admin/blocks")
def create_admin_block(
    payload: BlockedSlotCreate,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    user, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    try:
        with db() as conn:
            busy=conn.execute(
                """
                SELECT 1 FROM beauty_bookings
                WHERE master_id=1 AND booking_date=%s AND booking_time=%s
                  AND status IN ('pending','confirmed')
                LIMIT 1
                """,
                (payload.booking_date,payload.booking_time),
            ).fetchone()
            if busy:
                raise HTTPException(status_code=409,detail="На это время уже есть запись")
            row=conn.execute(
                """
                INSERT INTO beauty_blocked_slots(master_id,booking_date,booking_time,label,created_by)
                VALUES(1,%s,%s,%s,%s)
                RETURNING id,master_id,booking_date,booking_time,label
                """,
                (payload.booking_date,payload.booking_time,(payload.label or "").strip() or "Закрыто",int(user["id"])),
            ).fetchone()
    except UniqueViolation:
        raise HTTPException(status_code=409,detail="Это время уже закрыто")
    _clear_availability_cache()
    return {"ok":True,"block":{
        "id":str(row["id"]),"masterId":row["master_id"],
        "date":row["booking_date"].isoformat(),"time":row["booking_time"].strftime("%H:%M"),
        "label":row.get("label") or "Закрыто",
    }}


@router.delete("/admin/blocks/{block_id}")
def delete_admin_block(
    block_id:int,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    with db() as conn:
        row=conn.execute("DELETE FROM beauty_blocked_slots WHERE id=%s RETURNING id",(block_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404,detail="Закрытое окно не найдено")
    _clear_availability_cache()
    return {"ok":True}


@router.get("/admin/client-notes")
def list_admin_client_notes(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    with db() as conn:
        rows=conn.execute("SELECT client_key,note,updated_at FROM beauty_client_notes ORDER BY updated_at DESC").fetchall()
    return {"ok":True,"notes":{str(r["client_key"]):r.get("note") or "" for r in rows}}


@router.put("/admin/client-notes/{client_key:path}")
def update_admin_client_note(
    client_key:str,
    payload:ClientNoteUpdate,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    user, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    key=client_key.strip()[:220]
    if not key:
        raise HTTPException(status_code=400,detail="Не удалось определить клиента")
    note=payload.note.strip()
    with db() as conn:
        if note:
            conn.execute(
                """
                INSERT INTO beauty_client_notes(client_key,note,updated_by,updated_at)
                VALUES(%s,%s,%s,NOW())
                ON CONFLICT(client_key) DO UPDATE SET note=EXCLUDED.note,updated_by=EXCLUDED.updated_by,updated_at=NOW()
                """,
                (key,note,int(user["id"])),
            )
        else:
            conn.execute("DELETE FROM beauty_client_notes WHERE client_key=%s",(key,))
    return {"ok":True,"clientKey":key,"note":note}


@router.post("/bookings")
def create_booking(
    payload: BookingCreate,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    user, _ = _identity(x_telegram_init_data)
    uid = int(user["id"])
    first_name = str(user.get("first_name") or "")[:120] or None
    username = str(user.get("username") or "")[:120] or None

    try:
        with db() as conn:
            service = conn.execute(
                """
                SELECT id, name, price
                FROM beauty_services
                WHERE id=%s
                  AND is_active=TRUE
                  AND deleted_at IS NULL
                """,
                (payload.service_id,),
            ).fetchone()

            if not service:
                raise HTTPException(
                    status_code=409,
                    detail="Эта услуга сейчас недоступна для записи",
                )

            service_name = str(service["name"])

            if _slot_is_blocked(conn, payload.booking_date, payload.booking_time):
                raise HTTPException(
                    status_code=409,
                    detail="Это время больше недоступно. Выберите другое.",
                )
            if _slot_has_active_booking(conn, payload.booking_date, payload.booking_time):
                raise HTTPException(
                    status_code=409,
                    detail="Это время уже занято. Выберите другое время.",
                )

            row = conn.execute(
                """
                INSERT INTO beauty_bookings (
                    telegram_user_id, first_name, username, client_name, phone,
                    service_id, service_name,
                    master_id, master_name,
                    booking_date, booking_time, status, booking_source, price_snapshot
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,1,'Людмила',%s,%s,'pending','client',%s)
                RETURNING id, created_at
                """,
                (
                    uid,
                    first_name,
                    username,
                    payload.client_name.strip(),
                    payload.phone.strip(),
                    payload.service_id,
                    service_name,
                    payload.booking_date,
                    payload.booking_time,
                    service.get("price"),
                ),
            ).fetchone()
    except UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="Это время уже занято. Выберите другое время.",
        )

    _clear_availability_cache()

    staff_ids = _staff_chat_ids()
    if staff_ids:
        background_tasks.add_task(
            send_notifications,
            staff_ids,
            _new_booking_admin_text(
                booking_id=int(row["id"]),
                telegram_user_id=uid,
                first_name=first_name,
                username=username,
                client_name=payload.client_name.strip(),
                phone=payload.phone.strip(),
                service_name=service_name,
                master_name="Людмила",
                booking_date=payload.booking_date,
                booking_time=payload.booking_time,
            ),
        )

    return {
        "ok": True,
        "booking": {
            "id": str(row["id"]),
            "serviceId": payload.service_id,
            "serviceName": service_name,
            "masterId": 1,
            "masterName": "Людмила",
            "clientName": payload.client_name.strip(),
            "clientPhone": payload.phone.strip(),
            "date": payload.booking_date.isoformat(),
            "time": payload.booking_time.strftime("%H:%M"),
            "status": "pending",
            "createdAt": row["created_at"].isoformat(),
        },
    }


@router.delete("/bookings/{booking_id}")
def cancel_booking(
    booking_id: int,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    user, _ = _identity(x_telegram_init_data)
    uid = int(user["id"])

    with db() as conn:
        row = conn.execute(
            """
            UPDATE beauty_bookings
            SET status='cancelled', updated_at=NOW()
            WHERE id=%s
              AND telegram_user_id=%s
              AND status <> 'cancelled'
            RETURNING id, telegram_user_id, first_name, username, client_name, phone,
                      service_id, service_name, master_id, master_name,
                      booking_date, booking_time, status, created_at
            """,
            (booking_id, uid),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    _clear_availability_cache()

    staff_ids = _staff_chat_ids()
    if staff_ids:
        background_tasks.add_task(
            send_notifications,
            staff_ids,
            _client_cancelled_admin_text(row),
        )

    return {"ok": True}

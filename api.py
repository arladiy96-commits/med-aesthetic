from __future__ import annotations

import json
import secrets
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
from bot import make_admin_invite_link, make_certificate_gift_link, safe_set_role_menu_button, send_notification, send_notifications, send_test_reminder
from database import (
    create_admin_invite,
    db,
    decide_admin_access_request,
    grant_admin,
    list_admin_access_requests,
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


class ReminderTestRequest(BaseModel):
    kind: Literal["client_24h", "client_2h", "admin_1h", "admin_daily"]
    booking_id: Optional[int] = Field(default=None, gt=0)


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


class CertificateIssue(BaseModel):
    owner_telegram_user_id: int = Field(gt=0)
    amount: Optional[int] = Field(default=None, ge=0)
    service_name: Optional[str] = Field(default=None, max_length=180)
    title: str = Field(default="Подарочный сертификат", min_length=2, max_length=180)
    expires_at: Optional[date] = None


class CertificateGiftAccept(BaseModel):
    token: str = Field(min_length=16, max_length=220)


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
        "durationMinutes": int(row.get("duration_snapshot") or 60),
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


def _certificate_effective_status(row: dict) -> str:
    status = str(row.get("status") or "active")
    expires_at = row.get("expires_at")
    if status == "active" and expires_at and expires_at < date.today():
        return "expired"
    return status


def _certificate_owner_name(row: dict) -> str:
    return (
        row.get("owner_client_name")
        or " ".join(x for x in [row.get("owner_first_name"), row.get("owner_last_name")] if x)
        or (f"@{row.get('owner_username')}" if row.get("owner_username") else "")
        or f"Telegram {row.get('owner_telegram_user_id')}"
    )


def _certificate_payload(row: dict, *, include_qr: bool = False) -> dict:
    payload = {
        "id": str(row["id"]),
        "number": f"MA-{int(row['id']):06d}",
        "title": row.get("title") or "Подарочный сертификат",
        "amount": row.get("amount"),
        "serviceName": row.get("service_name") or "",
        "status": _certificate_effective_status(row),
        "ownerTelegramUserId": int(row["owner_telegram_user_id"]),
        "ownerName": _certificate_owner_name(row),
        "purchasedByTelegramUserId": int(row["purchased_by_telegram_user_id"]),
        "expiresAt": row["expires_at"].isoformat() if row.get("expires_at") else None,
        "usedAt": row["used_at"].isoformat() if row.get("used_at") else None,
        "usedBy": int(row["used_by"]) if row.get("used_by") is not None else None,
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
    }
    if include_qr:
        payload["qrToken"] = row.get("qr_token") or ""
    return payload


def _certificate_select(where_sql: str = "", params: tuple = ()) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT c.*,
                   u.client_name AS owner_client_name,
                   u.first_name AS owner_first_name,
                   u.last_name AS owner_last_name,
                   u.username AS owner_username
            FROM beauty_certificates c
            LEFT JOIN app_users u ON u.telegram_user_id=c.owner_telegram_user_id
            {where_sql}
            ORDER BY c.created_at DESC, c.id DESC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def _certificate_transfer_history(certificate_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT t.*,
                   f.client_name AS from_client_name, f.first_name AS from_first_name, f.last_name AS from_last_name, f.username AS from_username,
                   x.client_name AS to_client_name, x.first_name AS to_first_name, x.last_name AS to_last_name, x.username AS to_username
            FROM beauty_certificate_transfers t
            LEFT JOIN app_users f ON f.telegram_user_id=t.from_telegram_user_id
            LEFT JOIN app_users x ON x.telegram_user_id=t.to_telegram_user_id
            WHERE t.certificate_id=%s
            ORDER BY t.created_at DESC, t.id DESC
            """,
            (int(certificate_id),),
        ).fetchall()

    def label(prefix: str, row: dict, uid_key: str) -> str:
        return (
            row.get(prefix + "client_name")
            or " ".join(x for x in [row.get(prefix + "first_name"), row.get(prefix + "last_name")] if x)
            or (f"@{row.get(prefix + 'username')}" if row.get(prefix + "username") else "")
            or (f"Telegram {row.get(uid_key)}" if row.get(uid_key) else "")
        )

    result = []
    for raw in rows:
        row = dict(raw)
        state = "accepted" if row.get("accepted_at") else "cancelled" if row.get("cancelled_at") else "expired" if row.get("expires_at") and row["expires_at"] < __import__("datetime").datetime.now(row["expires_at"].tzinfo) else "pending"
        result.append({
            "id": str(row["id"]),
            "fromTelegramUserId": int(row["from_telegram_user_id"]),
            "fromName": label("from_", row, "from_telegram_user_id"),
            "toTelegramUserId": int(row["to_telegram_user_id"]) if row.get("to_telegram_user_id") else None,
            "toName": label("to_", row, "to_telegram_user_id"),
            "status": state,
            "createdAt": row["created_at"].isoformat(),
            "acceptedAt": row["accepted_at"].isoformat() if row.get("accepted_at") else None,
        })
    return result


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
            "clientName": app_user.get("client_name"),
            "phone": app_user.get("phone"),
            "actualRole": actual_role,
            "effectiveRole": effective_role,
        },
        "roleSwitch": {
            "creator": ["creator", "client"],
            "admin": ["admin"],
            "client": ["client"],
        }[actual_role],
    }


# ---------------------------------------------------------------------------
# Gift certificates
# ---------------------------------------------------------------------------
@router.get("/certificates/mine")
def my_certificates(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    user, _ = _identity(x_telegram_init_data)
    uid = int(user["id"])
    rows = _certificate_select("WHERE c.owner_telegram_user_id=%s", (uid,))

    with db() as conn:
        sent = conn.execute(
            """
            SELECT t.id, t.certificate_id, t.to_telegram_user_id, t.accepted_at,
                   c.title, c.amount, c.service_name,
                   u.client_name AS to_client_name, u.first_name AS to_first_name,
                   u.last_name AS to_last_name, u.username AS to_username
            FROM beauty_certificate_transfers t
            JOIN beauty_certificates c ON c.id=t.certificate_id
            LEFT JOIN app_users u ON u.telegram_user_id=t.to_telegram_user_id
            WHERE t.from_telegram_user_id=%s AND t.accepted_at IS NOT NULL
            ORDER BY t.accepted_at DESC, t.id DESC
            LIMIT 30
            """,
            (uid,),
        ).fetchall()

    sent_payload = []
    for raw in sent:
        row = dict(raw)
        to_name = (
            row.get("to_client_name")
            or " ".join(x for x in [row.get("to_first_name"), row.get("to_last_name")] if x)
            or (f"@{row.get('to_username')}" if row.get("to_username") else "")
            or f"Telegram {row.get('to_telegram_user_id')}"
        )
        sent_payload.append({
            "id": str(row["id"]),
            "certificateNumber": f"MA-{int(row['certificate_id']):06d}",
            "title": row.get("title") or "Подарочный сертификат",
            "amount": row.get("amount"),
            "serviceName": row.get("service_name") or "",
            "toName": to_name,
            "acceptedAt": row["accepted_at"].isoformat(),
        })

    return {
        "ok": True,
        "certificates": [_certificate_payload(r, include_qr=True) for r in rows],
        "sentTransfers": sent_payload,
    }


@router.post("/certificates/{certificate_id}/gift")
def create_certificate_gift(
    certificate_id: int,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    user, _ = _identity(x_telegram_init_data)
    uid = int(user["id"])
    raw_token = secrets.token_urlsafe(28)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM beauty_certificates
            WHERE id=%s AND owner_telegram_user_id=%s
            FOR UPDATE
            """,
            (certificate_id, uid),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Сертификат не найден")
        row = dict(row)
        if _certificate_effective_status(row) != "active":
            raise HTTPException(status_code=409, detail="Этот сертификат уже нельзя подарить")

        conn.execute(
            """
            UPDATE beauty_certificate_transfers
            SET cancelled_at=NOW()
            WHERE certificate_id=%s AND accepted_at IS NULL AND cancelled_at IS NULL
            """,
            (certificate_id,),
        )
        conn.execute(
            """
            INSERT INTO beauty_certificate_transfers(
                certificate_id, from_telegram_user_id, token_hash, expires_at
            )
            VALUES (%s,%s,%s,NOW()+INTERVAL '7 days')
            """,
            (certificate_id, uid, token_hash),
        )

    try:
        gift_link = make_certificate_gift_link(raw_token)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Не удалось создать Telegram-ссылку подарка") from exc

    return {"ok": True, "giftLink": gift_link, "expiresInDays": 7}


@router.get("/certificates/gift-preview")
def certificate_gift_preview(
    token: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _identity(x_telegram_init_data)
    token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
    with db() as conn:
        row = conn.execute(
            """
            SELECT t.id AS transfer_id, t.from_telegram_user_id, t.expires_at AS transfer_expires_at,
                   c.*,
                   u.client_name AS owner_client_name, u.first_name AS owner_first_name,
                   u.last_name AS owner_last_name, u.username AS owner_username
            FROM beauty_certificate_transfers t
            JOIN beauty_certificates c ON c.id=t.certificate_id
            LEFT JOIN app_users u ON u.telegram_user_id=t.from_telegram_user_id
            WHERE t.token_hash=%s
              AND t.accepted_at IS NULL
              AND t.cancelled_at IS NULL
              AND t.expires_at>NOW()
            """,
            (token_hash,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Ссылка подарка недействительна или уже использована")
    row = dict(row)
    if _certificate_effective_status(row) != "active":
        raise HTTPException(status_code=409, detail="Этот сертификат уже недействителен")

    sender = _certificate_owner_name(row)
    return {
        "ok": True,
        "gift": {
            "certificate": _certificate_payload(row, include_qr=False),
            "fromName": sender,
            "expiresAt": row["transfer_expires_at"].isoformat(),
        },
    }


@router.post("/certificates/gift-accept")
def accept_certificate_gift(
    payload: CertificateGiftAccept,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    user, _ = _identity(x_telegram_init_data)
    uid = int(user["id"])
    token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    new_qr_token = secrets.token_urlsafe(32)

    with db() as conn:
        row = conn.execute(
            """
            SELECT t.id AS transfer_id, t.from_telegram_user_id, t.expires_at AS transfer_expires_at,
                   t.accepted_at, t.cancelled_at, c.*
            FROM beauty_certificate_transfers t
            JOIN beauty_certificates c ON c.id=t.certificate_id
            WHERE t.token_hash=%s
            FOR UPDATE OF t, c
            """,
            (token_hash,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Ссылка подарка не найдена")
        row = dict(row)
        if row.get("accepted_at") or row.get("cancelled_at") or row["transfer_expires_at"] <= __import__("datetime").datetime.now(row["transfer_expires_at"].tzinfo):
            raise HTTPException(status_code=409, detail="Ссылка подарка недействительна или уже использована")
        if _certificate_effective_status(row) != "active":
            raise HTTPException(status_code=409, detail="Сертификат уже недействителен")
        if int(row["from_telegram_user_id"]) == uid:
            raise HTTPException(status_code=409, detail="Этот сертификат уже принадлежит вам")
        # The sender must still own the certificate at the exact acceptance moment.
        if int(row["owner_telegram_user_id"]) != int(row["from_telegram_user_id"]):
            raise HTTPException(status_code=409, detail="Сертификат уже был передан другому человеку")

        conn.execute(
            """
            UPDATE beauty_certificates
            SET owner_telegram_user_id=%s, qr_token=%s, updated_at=NOW()
            WHERE id=%s
            """,
            (uid, new_qr_token, row["id"]),
        )
        conn.execute(
            """
            UPDATE beauty_certificate_transfers
            SET to_telegram_user_id=%s, accepted_at=NOW()
            WHERE id=%s
            """,
            (uid, row["transfer_id"]),
        )

    fresh = _certificate_select("WHERE c.id=%s", (row["id"],))[0]
    background_tasks.add_task(
        send_notification,
        int(row["from_telegram_user_id"]),
        f"🎁 Сертификат {fresh['title']} принят получателем и передан новому владельцу.",
    )
    background_tasks.add_task(
        send_notification,
        uid,
        f"🎁 Сертификат {fresh['title']} теперь у вас. Откройте «Подарочные сертификаты» в MED AESTHETIC.",
    )
    return {"ok": True, "certificate": _certificate_payload(fresh, include_qr=True)}


@router.get("/admin/certificate-clients")
def admin_certificate_clients(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT telegram_user_id, first_name, last_name, username, client_name, phone, role
            FROM app_users
            ORDER BY COALESCE(NULLIF(client_name,''), NULLIF(first_name,''), username, telegram_user_id::text)
            """
        ).fetchall()
    users = []
    for raw in rows:
        row = dict(raw)
        name = row.get("client_name") or " ".join(x for x in [row.get("first_name"), row.get("last_name")] if x) or (f"@{row.get('username')}" if row.get("username") else f"Telegram {row['telegram_user_id']}")
        users.append({
            "telegramUserId": int(row["telegram_user_id"]),
            "name": name,
            "username": row.get("username") or "",
            "phone": row.get("phone") or "",
            "role": row.get("role") or "client",
        })
    return {"ok": True, "users": users}


@router.get("/admin/certificates")
def admin_certificates(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    rows = _certificate_select()
    return {"ok": True, "certificates": [_certificate_payload(r) for r in rows]}


@router.post("/admin/certificates")
def issue_certificate(
    payload: CertificateIssue,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    service_name = str(payload.service_name or "").strip() or None
    if not service_name and not (payload.amount is not None and int(payload.amount) > 0):
        raise HTTPException(status_code=400, detail="Укажите номинал или услугу сертификата")
    expires_at = payload.expires_at or (date.today() + timedelta(days=365))
    if expires_at < date.today():
        raise HTTPException(status_code=400, detail="Срок действия не может быть в прошлом")

    with db() as conn:
        owner = conn.execute(
            "SELECT telegram_user_id FROM app_users WHERE telegram_user_id=%s",
            (payload.owner_telegram_user_id,),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=404, detail="Пользователь ещё не открывал MED AESTHETIC в Telegram")
        row = conn.execute(
            """
            INSERT INTO beauty_certificates(
                owner_telegram_user_id, purchased_by_telegram_user_id,
                title, amount, service_name, status, qr_token, issued_by, expires_at
            )
            VALUES (%s,%s,%s,%s,%s,'active',%s,%s,%s)
            RETURNING id
            """,
            (
                payload.owner_telegram_user_id,
                payload.owner_telegram_user_id,
                payload.title.strip(),
                payload.amount,
                service_name,
                secrets.token_urlsafe(32),
                int(app_user["telegram_user_id"]),
                expires_at,
            ),
        ).fetchone()

    cert = _certificate_select("WHERE c.id=%s", (row["id"],))[0]
    background_tasks.add_task(
        send_notification,
        int(payload.owner_telegram_user_id),
        f"🎁 Вам выдан {cert['title']}. Сертификат уже доступен в MED AESTHETIC.",
    )
    return {"ok": True, "certificate": _certificate_payload(cert)}


@router.get("/admin/certificates/verify")
def verify_certificate(
    token: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    rows = _certificate_select("WHERE c.qr_token=%s", (str(token).strip(),))
    if not rows:
        raise HTTPException(status_code=404, detail="QR не относится к действующему сертификату MED AESTHETIC")
    cert = rows[0]
    return {
        "ok": True,
        "certificate": _certificate_payload(cert),
        "history": _certificate_transfer_history(int(cert["id"])),
    }


@router.get("/admin/certificates/{certificate_id}")
def admin_certificate_detail(
    certificate_id: int,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    rows = _certificate_select("WHERE c.id=%s", (certificate_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Сертификат не найден")
    cert = rows[0]
    return {
        "ok": True,
        "certificate": _certificate_payload(cert),
        "history": _certificate_transfer_history(certificate_id),
    }


@router.post("/admin/certificates/{certificate_id}/redeem")
def redeem_certificate(
    certificate_id: int,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM beauty_certificates WHERE id=%s FOR UPDATE",
            (certificate_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Сертификат не найден")
        row = dict(row)
        status = _certificate_effective_status(row)
        if status == "expired":
            raise HTTPException(status_code=409, detail="Срок действия сертификата истёк")
        if status == "used":
            raise HTTPException(status_code=409, detail="Сертификат уже был использован")
        if status != "active":
            raise HTTPException(status_code=409, detail="Сертификат недействителен")

        conn.execute(
            """
            UPDATE beauty_certificates
            SET status='used', used_at=NOW(), used_by=%s, updated_at=NOW()
            WHERE id=%s
            """,
            (int(app_user["telegram_user_id"]), certificate_id),
        )
        conn.execute(
            """
            UPDATE beauty_certificate_transfers
            SET cancelled_at=NOW()
            WHERE certificate_id=%s AND accepted_at IS NULL AND cancelled_at IS NULL
            """,
            (certificate_id,),
        )

    fresh = _certificate_select("WHERE c.id=%s", (certificate_id,))[0]
    background_tasks.add_task(
        send_notification,
        int(fresh["owner_telegram_user_id"]),
        f"✅ Сертификат {fresh['title']} отмечен как использованный в MED AESTHETIC.",
    )
    return {
        "ok": True,
        "certificate": _certificate_payload(fresh),
        "history": _certificate_transfer_history(certificate_id),
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


@router.get("/admin-access-requests")
def admin_access_requests(
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)

    requests = []
    for row in list_admin_access_requests("pending"):
        requests.append(
            {
                "id": int(row["id"]),
                "telegramUserId": int(row["telegram_user_id"]),
                "firstName": row.get("first_name"),
                "lastName": row.get("last_name"),
                "username": row.get("username"),
                "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
            }
        )
    return {"ok": True, "requests": requests}


@router.post("/admin-access-requests/{request_id}/approve")
def approve_admin_access_request(
    request_id: int,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)

    result = decide_admin_access_request(
        request_id, "approved", int(app_user["telegram_user_id"])
    )
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "not_found":
            raise HTTPException(status_code=404, detail="Заявка не найдена")
        if reason == "already_decided":
            raise HTTPException(status_code=409, detail="Заявка уже обработана")
        raise HTTPException(status_code=400, detail="Не удалось одобрить заявку")

    telegram_user_id = int(result["request"]["telegram_user_id"])
    background_tasks.add_task(safe_set_role_menu_button, telegram_user_id, "admin")
    background_tasks.add_task(
        send_notification,
        telegram_user_id,
        "✅ Ваша заявка одобрена.\n\nВам предоставлены права администратора MED AESTHETIC.",
    )
    return {"ok": True}


@router.post("/admin-access-requests/{request_id}/reject")
def reject_admin_access_request(
    request_id: int,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)

    result = decide_admin_access_request(
        request_id, "rejected", int(app_user["telegram_user_id"])
    )
    if not result.get("ok"):
        reason = result.get("reason")
        if reason == "not_found":
            raise HTTPException(status_code=404, detail="Заявка не найдена")
        if reason == "already_decided":
            raise HTTPException(status_code=409, detail="Заявка уже обработана")
        raise HTTPException(status_code=400, detail="Не удалось отклонить заявку")

    telegram_user_id = int(result["request"]["telegram_user_id"])
    background_tasks.add_task(
        send_notification,
        telegram_user_id,
        "Заявка на права администратора MED AESTHETIC отклонена.",
    )
    return {"ok": True}


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
    _clear_availability_cache()
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
    _clear_availability_cache()
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
    _clear_availability_cache()
    return {"ok": True}


AVAILABILITY_CACHE_TTL_SECONDS = 15.0
DEFAULT_SERVICE_DURATION_MINUTES = 60
BLOCK_DURATION_MINUTES = 60
WORKDAY_START_MINUTES = 10 * 60
WORKDAY_END_MINUTES = 21 * 60
BOOKING_START_MINUTES = tuple(range(WORKDAY_START_MINUTES, 20 * 60 + 1, 60))

_availability_cache_lock = RLock()
_availability_cache: dict[tuple[int, str, int, int], tuple[float, dict]] = {}


def _clear_availability_cache() -> None:
    with _availability_cache_lock:
        _availability_cache.clear()


def _duration_minutes(value) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = DEFAULT_SERVICE_DURATION_MINUTES
    return max(1, min(minutes, 1440))


def _clock_minutes(value: time) -> int:
    return int(value.hour) * 60 + int(value.minute)


def _clock_text(total_minutes: int) -> str:
    total_minutes = max(0, int(total_minutes))
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _overlaps(start_a: int, duration_a: int, start_b: int, duration_b: int) -> bool:
    end_a = start_a + _duration_minutes(duration_a)
    end_b = start_b + _duration_minutes(duration_b)
    return start_a < end_b and end_a > start_b


def _lock_master_day(conn, booking_date: date, master_id: int = 1) -> None:
    # Serializes slot-changing operations for one master/day.
    # This protects against two concurrent requests that start at different
    # clock times but whose service durations overlap.
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (int(master_id), int(booking_date.toordinal())),
    )


def _slot_conflict_reason(
    conn,
    booking_date: date,
    booking_time: time,
    duration_minutes: int,
    *,
    master_id: int = 1,
    exclude_booking_id: int | None = None,
    include_blocks: bool = True,
) -> str | None:
    candidate_start = _clock_minutes(booking_time)
    candidate_duration = _duration_minutes(duration_minutes)
    candidate_end = candidate_start + candidate_duration

    if candidate_start < WORKDAY_START_MINUTES:
        return "Это время находится вне рабочего графика"
    if candidate_end > WORKDAY_END_MINUTES:
        return (
            f"Услуга длится {candidate_duration} мин и не помещается "
            f"до конца рабочего дня"
        )

    if exclude_booking_id is None:
        booking_rows = conn.execute(
            """
            SELECT b.booking_time,
                   COALESCE(b.duration_snapshot, s.duration, %s) AS duration_minutes
            FROM beauty_bookings b
            LEFT JOIN beauty_services s ON s.id=b.service_id
            WHERE b.master_id=%s
              AND b.booking_date=%s
              AND b.status IN ('pending','confirmed')
            """,
            (DEFAULT_SERVICE_DURATION_MINUTES, master_id, booking_date),
        ).fetchall()
    else:
        booking_rows = conn.execute(
            """
            SELECT b.booking_time,
                   COALESCE(b.duration_snapshot, s.duration, %s) AS duration_minutes
            FROM beauty_bookings b
            LEFT JOIN beauty_services s ON s.id=b.service_id
            WHERE b.master_id=%s
              AND b.booking_date=%s
              AND b.status IN ('pending','confirmed')
              AND b.id<>%s
            """,
            (
                DEFAULT_SERVICE_DURATION_MINUTES,
                master_id,
                booking_date,
                exclude_booking_id,
            ),
        ).fetchall()

    for row in booking_rows:
        existing_start = _clock_minutes(row["booking_time"])
        existing_duration = _duration_minutes(row.get("duration_minutes"))
        if _overlaps(
            candidate_start,
            candidate_duration,
            existing_start,
            existing_duration,
        ):
            return "Это время пересекается с другой записью"

    if include_blocks:
        block_rows = conn.execute(
            """
            SELECT booking_time
            FROM beauty_blocked_slots
            WHERE master_id=%s AND booking_date=%s
            """,
            (master_id, booking_date),
        ).fetchall()
        for row in block_rows:
            block_start = _clock_minutes(row["booking_time"])
            if _overlaps(
                candidate_start,
                candidate_duration,
                block_start,
                BLOCK_DURATION_MINUTES,
            ):
                return "Это время пересекается с закрытым окном"

    return None


def _availability_range_payload(
    start_date: date,
    days: int,
    *,
    service_id: int = 0,
    master_id: int = 1,
) -> dict:
    days = max(1, min(int(days), 31))
    end_date = start_date + timedelta(days=days - 1)
    service_id = int(service_id or 0)
    cache_key = (int(master_id), start_date.isoformat(), days, service_id)
    now = monotonic()

    # Single-flight guard: simultaneous users requesting the same calendar
    # reuse one Neon result during the 15-second cache window.
    with _availability_cache_lock:
        cached = _availability_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        with db() as conn:
            if service_id > 0:
                service = conn.execute(
                    """
                    SELECT id, duration
                    FROM beauty_services
                    WHERE id=%s AND is_active=TRUE AND deleted_at IS NULL
                    """,
                    (service_id,),
                ).fetchone()
                if not service:
                    raise HTTPException(
                        status_code=409,
                        detail="Эта услуга сейчас недоступна для записи",
                    )
                selected_duration = _duration_minutes(service.get("duration"))
            else:
                # Compatibility mode for an old client that did not send service_id.
                selected_duration = DEFAULT_SERVICE_DURATION_MINUTES

            rows = conn.execute(
                """
                SELECT b.booking_date,
                       b.booking_time,
                       COALESCE(b.duration_snapshot, s.duration, %s) AS duration_minutes
                FROM beauty_bookings b
                LEFT JOIN beauty_services s ON s.id=b.service_id
                WHERE b.master_id=%s
                  AND b.booking_date BETWEEN %s AND %s
                  AND b.status IN ('pending','confirmed')

                UNION ALL

                SELECT booking_date,
                       booking_time,
                       %s AS duration_minutes
                FROM beauty_blocked_slots
                WHERE master_id=%s
                  AND booking_date BETWEEN %s AND %s

                ORDER BY booking_date, booking_time
                """,
                (
                    DEFAULT_SERVICE_DURATION_MINUTES,
                    master_id,
                    start_date,
                    end_date,
                    BLOCK_DURATION_MINUTES,
                    master_id,
                    start_date,
                    end_date,
                ),
            ).fetchall()

        occupied_by_day: dict[str, list[tuple[int, int]]] = {
            (start_date + timedelta(days=i)).isoformat(): []
            for i in range(days)
        }
        for row in rows:
            day_key = row["booking_date"].isoformat()
            if day_key in occupied_by_day:
                occupied_by_day[day_key].append(
                    (
                        _clock_minutes(row["booking_time"]),
                        _duration_minutes(row.get("duration_minutes")),
                    )
                )

        availability: dict[str, list[str]] = {}
        for day_key, intervals in occupied_by_day.items():
            unavailable: list[str] = []
            for candidate_start in BOOKING_START_MINUTES:
                candidate_end = candidate_start + selected_duration
                blocked = (
                    candidate_end > WORKDAY_END_MINUTES
                    or any(
                        _overlaps(
                            candidate_start,
                            selected_duration,
                            occupied_start,
                            occupied_duration,
                        )
                        for occupied_start, occupied_duration in intervals
                    )
                )
                if blocked:
                    unavailable.append(_clock_text(candidate_start))
            availability[day_key] = unavailable

        payload = {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "days": days,
            "serviceId": service_id or None,
            "serviceDuration": selected_duration,
            "cacheTtlSeconds": int(AVAILABILITY_CACHE_TTL_SECONDS),
            "availability": availability,
        }
        _availability_cache[cache_key] = (
            monotonic() + AVAILABILITY_CACHE_TTL_SECONDS,
            payload,
        )
        return payload


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


@router.post("/admin/reminder-tests/send")
def send_admin_reminder_test(
    payload: ReminderTestRequest,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    telegram_user, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)
    try:
        result = send_test_reminder(payload.kind, payload.booking_id, int(telegram_user["id"]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось отправить тест: {str(exc)[:180]}") from exc
    return result


@router.get("/availability-range")
def booking_availability_range(
    start_date: date,
    days: int = 14,
    service_id: int = 0,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    # Availability depends on the duration of the service the client selected.
    _identity(x_telegram_init_data)
    if days < 1 or days > 31:
        raise HTTPException(status_code=400, detail="days должен быть от 1 до 31")
    payload = _availability_range_payload(
        start_date,
        days,
        service_id=service_id,
        master_id=1,
    )
    return {"ok": True, **payload}


@router.get("/availability")
def booking_availability(
    booking_date: date,
    service_id: int = 0,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    # Compatibility endpoint for older clients.
    _identity(x_telegram_init_data)
    payload = _availability_range_payload(
        booking_date,
        1,
        service_id=service_id,
        master_id=1,
    )
    return {
        "ok": True,
        "date": booking_date.isoformat(),
        "unavailable": payload["availability"].get(booking_date.isoformat(), []),
        "serviceDuration": payload["serviceDuration"],
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
                   booking_date, booking_time, status, created_at, duration_snapshot
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
                   note, booking_source, price_snapshot, duration_snapshot
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
                SELECT id, name, price, duration
                FROM beauty_services
                WHERE id=%s AND deleted_at IS NULL
                """,
                (payload.service_id,),
            ).fetchone()
            if not service:
                raise HTTPException(status_code=409, detail="Услуга недоступна")

            service_duration = _duration_minutes(service.get("duration"))
            _lock_master_day(conn, payload.booking_date, master_id=1)
            conflict = _slot_conflict_reason(
                conn,
                payload.booking_date,
                payload.booking_time,
                service_duration,
                master_id=1,
            )
            if conflict:
                raise HTTPException(status_code=409, detail=conflict)

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
                    booking_date, booking_time, status, note, booking_source,
                    price_snapshot, duration_snapshot
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,1,'Людмила',%s,%s,'confirmed',%s,'admin',%s,%s)
                RETURNING id, telegram_user_id, first_name, username, client_name, phone,
                          service_id, service_name, master_id, master_name,
                          booking_date, booking_time, status, created_at,
                          note, booking_source, price_snapshot, duration_snapshot
                """,
                (
                    telegram_user_id, first_name, username, client_name, phone,
                    payload.service_id, str(service["name"]),
                    payload.booking_date, payload.booking_time, note,
                    service.get("price"), service_duration,
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
                       note, booking_source, price_snapshot, duration_snapshot
                FROM beauty_bookings WHERE id=%s
                """,
                (booking_id,),
            ).fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Запись не найдена")

            new_date = changes.get("booking_date", current["booking_date"])
            new_time = changes.get("booking_time", current["booking_time"])
            new_status = changes.get("status", current["status"])
            moved = new_date != current["booking_date"] or new_time != current["booking_time"]
            was_active = current["status"] in ("pending", "confirmed")
            will_be_active = new_status in ("pending", "confirmed")
            duration_minutes = _duration_minutes(current.get("duration_snapshot"))

            if will_be_active and (moved or not was_active):
                _lock_master_day(conn, new_date, master_id=1)
                conflict = _slot_conflict_reason(
                    conn,
                    new_date,
                    new_time,
                    duration_minutes,
                    master_id=1,
                    exclude_booking_id=booking_id,
                )
                if conflict:
                    raise HTTPException(status_code=409, detail=conflict)

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
                          note, booking_source, price_snapshot, duration_snapshot
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
            _lock_master_day(conn, payload.booking_date, master_id=1)
            conflict = _slot_conflict_reason(
                conn,
                payload.booking_date,
                payload.booking_time,
                BLOCK_DURATION_MINUTES,
                master_id=1,
                include_blocks=True,
            )
            if conflict:
                raise HTTPException(status_code=409, detail=conflict)
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



@router.delete("/admin/bookings/{booking_id}")
def creator_delete_booking(
    booking_id: int,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)

    with db() as conn:
        row = conn.execute(
            "SELECT id FROM beauty_bookings WHERE id=%s",
            (booking_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Запись не найдена")

        conn.execute(
            "DELETE FROM beauty_reminder_log WHERE booking_id=%s",
            (booking_id,),
        )
        conn.execute(
            "DELETE FROM beauty_bookings WHERE id=%s",
            (booking_id,),
        )

    _clear_availability_cache()
    return {"ok": True, "deletedBookingId": str(booking_id)}


@router.delete("/admin/clients/{client_key:path}")
def creator_delete_client(
    client_key: str,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)

    key = str(client_key or "").strip()[:220]
    if not key:
        raise HTTPException(status_code=400, detail="Не удалось определить клиента")

    with db() as conn:
        booking_rows = []
        telegram_user_id: int | None = None

        if key.startswith("tg:"):
            try:
                telegram_user_id = int(key[3:])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Некорректный Telegram ID") from exc

            booking_rows = conn.execute(
                """
                SELECT id, phone
                FROM beauty_bookings
                WHERE telegram_user_id=%s
                """,
                (telegram_user_id,),
            ).fetchall()

        elif key.startswith("phone:"):
            digits = "".join(ch for ch in key[6:] if ch.isdigit())
            if not digits:
                raise HTTPException(status_code=400, detail="Некорректный номер клиента")

            booking_rows = conn.execute(
                """
                SELECT id, phone
                FROM beauty_bookings
                WHERE regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g')=%s
                  AND telegram_user_id IS NULL
                """,
                (digits,),
            ).fetchall()

        elif key.startswith("name:"):
            name = key[5:].strip().lower()
            if not name:
                raise HTTPException(status_code=400, detail="Некорректное имя клиента")

            booking_rows = conn.execute(
                """
                SELECT id, phone
                FROM beauty_bookings
                WHERE telegram_user_id IS NULL
                  AND NULLIF(regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g'),'') IS NULL
                  AND lower(trim(COALESCE(client_name,'')))=%s
                """,
                (name,),
            ).fetchall()
        else:
            raise HTTPException(status_code=400, detail="Некорректный ключ клиента")

        booking_ids = [int(r["id"]) for r in booking_rows]
        legacy_phone_keys = {
            "phone:" + "".join(ch for ch in str(r.get("phone") or "") if ch.isdigit())
            for r in booking_rows
            if "".join(ch for ch in str(r.get("phone") or "") if ch.isdigit())
        }

        if booking_ids:
            conn.execute(
                "DELETE FROM beauty_reminder_log WHERE booking_id = ANY(%s)",
                (booking_ids,),
            )
            conn.execute(
                "DELETE FROM beauty_bookings WHERE id = ANY(%s)",
                (booking_ids,),
            )

        note_keys = {key, *legacy_phone_keys}
        if telegram_user_id is not None:
            note_keys.add(f"tg:{telegram_user_id}")

        if note_keys:
            conn.execute(
                "DELETE FROM beauty_client_notes WHERE client_key = ANY(%s)",
                (list(note_keys),),
            )

        # Keep the identity/role row itself. Only remove business contact data.
        # If this Telegram user returns later, the account is recreated as the
        # same identity rather than becoming a different client.
        if telegram_user_id is not None:
            conn.execute(
                """
                UPDATE app_users
                SET client_name=NULL,
                    phone=NULL,
                    updated_at=NOW()
                WHERE telegram_user_id=%s
                """,
                (telegram_user_id,),
            )

    _clear_availability_cache()
    return {
        "ok": True,
        "clientKey": key,
        "deletedBookings": len(booking_ids),
    }


@router.delete("/admin/test-data")
def creator_clear_test_data(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_creator(app_user)

    with db() as conn:
        booking_count = conn.execute(
            "SELECT COUNT(*) AS n FROM beauty_bookings"
        ).fetchone()["n"]
        note_count = conn.execute(
            "SELECT COUNT(*) AS n FROM beauty_client_notes"
        ).fetchone()["n"]
        certificate_count = conn.execute(
            "SELECT COUNT(*) AS n FROM beauty_certificates"
        ).fetchone()["n"]

        # Deliberately preserve:
        # services, blocked schedule slots, creator/admin roles, access settings.
        conn.execute("DELETE FROM beauty_certificate_transfers")
        conn.execute("DELETE FROM beauty_certificates")
        conn.execute("DELETE FROM beauty_reminder_log")
        conn.execute("DELETE FROM beauty_client_notes")
        conn.execute("DELETE FROM beauty_bookings")
        conn.execute(
            """
            UPDATE app_users
            SET client_name=NULL,
                phone=NULL,
                updated_at=NOW()
            WHERE client_name IS NOT NULL OR phone IS NOT NULL
            """
        )

        # Clean launch: after a complete pre-launch wipe, booking numbering
        # starts from 1 again when PostgreSQL sequence is available.
        conn.execute(
            """
            SELECT setval(
                pg_get_serial_sequence('beauty_bookings','id'),
                1,
                false
            )
            """
        )

    _clear_availability_cache()
    return {
        "ok": True,
        "deletedBookings": int(booking_count or 0),
        "deletedClientNotes": int(note_count or 0),
        "deletedCertificates": int(certificate_count or 0),
    }


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
    client_name = payload.client_name.strip()
    phone = payload.phone.strip()

    try:
        with db() as conn:
            service = conn.execute(
                """
                SELECT id, name, price, duration
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
            service_duration = _duration_minutes(service.get("duration"))

            _lock_master_day(conn, payload.booking_date, master_id=1)
            conflict = _slot_conflict_reason(
                conn,
                payload.booking_date,
                payload.booking_time,
                service_duration,
                master_id=1,
            )
            if conflict:
                raise HTTPException(
                    status_code=409,
                    detail=conflict + ". Выберите другое время.",
                )

            # Telegram ID is the client's permanent identity.
            # Name/phone are editable profile data and always keep the latest
            # values submitted by this same Telegram user.
            conn.execute(
                """
                UPDATE app_users
                SET client_name=%s,
                    phone=%s,
                    updated_at=NOW()
                WHERE telegram_user_id=%s
                """,
                (client_name, phone, uid),
            )

            row = conn.execute(
                """
                INSERT INTO beauty_bookings (
                    telegram_user_id, first_name, username, client_name, phone,
                    service_id, service_name,
                    master_id, master_name,
                    booking_date, booking_time, status, booking_source,
                    price_snapshot, duration_snapshot
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,1,'Людмила',%s,%s,'pending','client',%s,%s)
                RETURNING id, created_at
                """,
                (
                    uid,
                    first_name,
                    username,
                    client_name,
                    phone,
                    payload.service_id,
                    service_name,
                    payload.booking_date,
                    payload.booking_time,
                    service.get("price"),
                    service_duration,
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
                client_name=client_name,
                phone=phone,
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
            "durationMinutes": service_duration,
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
                      booking_date, booking_time, status, created_at, duration_snapshot
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

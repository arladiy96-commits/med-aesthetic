from __future__ import annotations

from datetime import date, time
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation

from auth import validate_telegram_init_data
from bot import make_admin_invite_link, send_notification, send_notifications
from database import (
    create_admin_invite,
    db,
    list_admins,
    resolve_effective_role,
    revoke_admin,
    upsert_app_user,
)

router = APIRouter()


class BookingCreate(BaseModel):
    service_id: int = Field(gt=0)
    service_name: str = Field(min_length=1, max_length=180)
    master_id: Optional[int] = Field(default=None, gt=0)
    master_name: Optional[str] = Field(default=None, max_length=120)
    booking_date: date
    booking_time: time


class BookingStatusUpdate(BaseModel):
    status: Literal["pending", "confirmed", "completed", "cancelled", "no_show"]


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
        result.update(
            {
                "telegramUserId": int(row["telegram_user_id"]),
                "clientFirstName": first_name,
                "clientUsername": username,
                "clientName": first_name or (f"@{username}" if username else "Клиент"),
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


def _client_label(first_name: str | None, username: str | None, telegram_user_id: int) -> str:
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
    service_name: str,
    master_name: str | None,
    booking_date: date,
    booking_time: time,
) -> str:
    client = _client_label(first_name, username, telegram_user_id)
    return (
        "🆕 Новая запись MED AESTHETIC\n\n"
        f"Клиент: {client}\n"
        f"Услуга: {service_name}\n"
        f"Мастер: {master_name or 'Людмила'}\n"
        f"Дата: {_date_ru(booking_date)}\n"
        f"Время: {booking_time.strftime('%H:%M')}\n"
        f"Запись #{booking_id}"
    )


def _booking_created_client_text(
    *,
    service_name: str,
    master_name: str | None,
    booking_date: date,
    booking_time: time,
) -> str:
    return (
        "✅ Запись создана\n\n"
        f"{service_name}\n"
        f"Мастер: {master_name or 'Людмила'}\n"
        f"{_date_ru(booking_date)} в {booking_time.strftime('%H:%M')}\n\n"
        "Если статус записи изменится, я сообщу здесь."
    )


def _status_client_text(row: dict) -> str:
    status = str(row["status"])
    title = {
        "pending": "⏳ Запись ожидает подтверждения",
        "confirmed": "✅ Запись подтверждена",
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
        row.get("first_name"),
        row.get("username"),
        int(row["telegram_user_id"]),
    )
    return (
        "⚠️ Клиент отменил запись\n\n"
        f"Клиент: {client}\n"
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
            "creator": ["creator", "admin", "client"],
            "admin": ["admin", "client"],
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

    return {"ok": True}


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
            SELECT id, telegram_user_id, first_name, username,
                   service_id, service_name, master_id, master_name,
                   booking_date, booking_time, status, created_at
            FROM beauty_bookings
            ORDER BY booking_date ASC, booking_time ASC, id ASC
            """
        ).fetchall()

    return {
        "ok": True,
        "bookings": [_booking_payload(r, include_client=True) for r in rows],
    }


@router.patch("/admin/bookings/{booking_id}")
def update_admin_booking(
    booking_id: int,
    payload: BookingStatusUpdate,
    background_tasks: BackgroundTasks,
    x_telegram_init_data: str | None = Header(
        default=None, alias="X-Telegram-Init-Data"
    ),
):
    _, app_user = _identity(x_telegram_init_data)
    _require_staff(app_user)

    with db() as conn:
        row = conn.execute(
            """
            UPDATE beauty_bookings
            SET status=%s, updated_at=NOW()
            WHERE id=%s
            RETURNING id, telegram_user_id, first_name, username,
                      service_id, service_name, master_id, master_name,
                      booking_date, booking_time, status, created_at
            """,
            (payload.status, booking_id),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    background_tasks.add_task(
        send_notification,
        int(row["telegram_user_id"]),
        _status_client_text(row),
    )

    return {
        "ok": True,
        "booking": _booking_payload(row, include_client=True),
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

    try:
        with db() as conn:
            row = conn.execute(
                """
                INSERT INTO beauty_bookings (
                    telegram_user_id, first_name, username,
                    service_id, service_name,
                    master_id, master_name,
                    booking_date, booking_time, status
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'confirmed')
                RETURNING id, created_at
                """,
                (
                    uid,
                    first_name,
                    username,
                    payload.service_id,
                    payload.service_name,
                    payload.master_id,
                    payload.master_name,
                    payload.booking_date,
                    payload.booking_time,
                ),
            ).fetchone()
    except UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="Это время уже занято. Выберите другое время.",
        )

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
                service_name=payload.service_name,
                master_name=payload.master_name,
                booking_date=payload.booking_date,
                booking_time=payload.booking_time,
            ),
        )

    background_tasks.add_task(
        send_notification,
        uid,
        _booking_created_client_text(
            service_name=payload.service_name,
            master_name=payload.master_name,
            booking_date=payload.booking_date,
            booking_time=payload.booking_time,
        ),
    )

    return {
        "ok": True,
        "booking": {
            "id": str(row["id"]),
            "serviceId": payload.service_id,
            "serviceName": payload.service_name,
            "masterId": payload.master_id,
            "masterName": payload.master_name,
            "date": payload.booking_date.isoformat(),
            "time": payload.booking_time.strftime("%H:%M"),
            "status": "confirmed",
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
            RETURNING id, telegram_user_id, first_name, username,
                      service_id, service_name, master_id, master_name,
                      booking_date, booking_time, status, created_at
            """,
            (booking_id, uid),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    staff_ids = _staff_chat_ids()
    if staff_ids:
        background_tasks.add_task(
            send_notifications,
            staff_ids,
            _client_cancelled_admin_text(row),
        )

    return {"ok": True}

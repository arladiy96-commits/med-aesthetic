from __future__ import annotations

from datetime import date, time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation

from auth import validate_telegram_init_data
from database import db

router = APIRouter()


class BookingCreate(BaseModel):
    service_id: int = Field(gt=0)
    service_name: str = Field(min_length=1, max_length=180)
    master_id: Optional[int] = Field(default=None, gt=0)
    master_name: Optional[str] = Field(default=None, max_length=120)
    booking_date: date
    booking_time: time


def _user(init_data: str | None) -> dict:
    return validate_telegram_init_data(init_data or "")


@router.get("/bookings")
def list_bookings(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    user = _user(x_telegram_init_data)
    uid = int(user["id"])

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, service_id, service_name, master_id, master_name,
                   booking_date, booking_time, status, created_at
            FROM beauty_bookings
            WHERE telegram_user_id = %s
              AND status <> 'cancelled'
            ORDER BY booking_date ASC, booking_time ASC, id ASC
            """,
            (uid,),
        ).fetchall()

    return {
        "ok": True,
        "bookings": [
            {
                "id": str(r["id"]),
                "serviceId": r["service_id"],
                "serviceName": r["service_name"],
                "masterId": r["master_id"],
                "masterName": r["master_name"],
                "date": r["booking_date"].isoformat(),
                "time": r["booking_time"].strftime("%H:%M"),
                "status": r["status"],
                "createdAt": r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }


@router.post("/bookings")
def create_booking(
    payload: BookingCreate,
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    user = _user(x_telegram_init_data)
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
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
):
    user = _user(x_telegram_init_data)
    uid = int(user["id"])

    with db() as conn:
        row = conn.execute(
            """
            UPDATE beauty_bookings
            SET status='cancelled', updated_at=NOW()
            WHERE id=%s AND telegram_user_id=%s AND status <> 'cancelled'
            RETURNING id
            """,
            (booking_id, uid),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    return {"ok": True}

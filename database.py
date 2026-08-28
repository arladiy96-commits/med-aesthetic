from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    return url


@contextmanager
def db():
    conn = psycopg.connect(
        get_database_url(),
        row_factory=dict_row,
        connect_timeout=10,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beauty_bookings (
                id BIGSERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                first_name TEXT,
                username TEXT,
                service_id INTEGER NOT NULL,
                service_name TEXT NOT NULL,
                master_id INTEGER,
                master_name TEXT,
                booking_date DATE NOT NULL,
                booking_time TIME NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_beauty_bookings_user
            ON beauty_bookings (telegram_user_id, booking_date, booking_time)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_beauty_master_slot_active
            ON beauty_bookings (master_id, booking_date, booking_time)
            WHERE master_id IS NOT NULL AND status IN ('confirmed', 'pending')
            """
        )


def database_status() -> dict:
    try:
        with db() as conn:
            row = conn.execute("SELECT current_database() AS name, NOW() AS now").fetchone()
        return {"ok": True, "name": row["name"]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180]}

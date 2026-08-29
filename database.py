from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


VALID_ROLES = {"creator", "admin", "client"}


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    return url


def get_creator_telegram_id() -> int | None:
    value = os.getenv("CREATOR_TELEGRAM_ID", "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


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
            CREATE TABLE IF NOT EXISTS app_users (
                telegram_user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                role TEXT NOT NULL DEFAULT 'client',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT app_users_role_check
                    CHECK (role IN ('creator', 'admin', 'client'))
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_invites (
                id BIGSERIAL PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                created_by BIGINT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                used_at TIMESTAMPTZ,
                used_by BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_admin_invites_active
            ON admin_invites (expires_at, used_at)
            """
        )

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
            row = conn.execute(
                "SELECT current_database() AS name, NOW() AS now"
            ).fetchone()
        return {"ok": True, "name": row["name"]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180]}


def upsert_app_user(telegram_user: dict) -> dict:
    telegram_user_id = int(telegram_user["id"])
    creator_id = get_creator_telegram_id()

    first_name = str(telegram_user.get("first_name") or "")[:120] or None
    last_name = str(telegram_user.get("last_name") or "")[:120] or None
    username = str(telegram_user.get("username") or "")[:120] or None

    with db() as conn:
        current = conn.execute(
            """
            SELECT telegram_user_id, first_name, last_name, username, role,
                   created_at, updated_at, last_seen_at
            FROM app_users
            WHERE telegram_user_id = %s
            """,
            (telegram_user_id,),
        ).fetchone()

        desired_role = "creator" if creator_id == telegram_user_id else "client"

        if current:
            role = current["role"]
            if creator_id == telegram_user_id:
                role = "creator"

            row = conn.execute(
                """
                UPDATE app_users
                SET first_name=%s,
                    last_name=%s,
                    username=%s,
                    role=%s,
                    updated_at=NOW(),
                    last_seen_at=NOW()
                WHERE telegram_user_id=%s
                RETURNING telegram_user_id, first_name, last_name, username,
                          role, created_at, updated_at, last_seen_at
                """,
                (
                    first_name,
                    last_name,
                    username,
                    role,
                    telegram_user_id,
                ),
            ).fetchone()
            return dict(row)

        row = conn.execute(
            """
            INSERT INTO app_users (
                telegram_user_id, first_name, last_name, username, role
            )
            VALUES (%s,%s,%s,%s,%s)
            RETURNING telegram_user_id, first_name, last_name, username,
                      role, created_at, updated_at, last_seen_at
            """,
            (
                telegram_user_id,
                first_name,
                last_name,
                username,
                desired_role,
            ),
        ).fetchone()
        return dict(row)


def get_app_user(telegram_user_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT telegram_user_id, first_name, last_name, username, role,
                   created_at, updated_at, last_seen_at
            FROM app_users
            WHERE telegram_user_id=%s
            """,
            (telegram_user_id,),
        ).fetchone()
    return dict(row) if row else None


def resolve_effective_role(actual_role: str, requested_role: str | None) -> str:
    if not requested_role:
        return actual_role

    requested_role = requested_role.strip().lower()
    if requested_role not in VALID_ROLES:
        return actual_role

    if actual_role == "creator":
        return "client" if requested_role == "client" else "creator"
    if actual_role == "admin":
        return "admin"
    return "client"


def create_admin_invite(created_by: int, lifetime_hours: int = 168) -> dict:
    raw_token = secrets.token_urlsafe(18)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    with db() as conn:
        row = conn.execute(
            """
            INSERT INTO admin_invites (
                token_hash, created_by, expires_at
            )
            VALUES (
                %s,
                %s,
                NOW() + (%s * INTERVAL '1 hour')
            )
            RETURNING id, expires_at, created_at
            """,
            (token_hash, created_by, lifetime_hours),
        ).fetchone()

    return {
        "token": raw_token,
        "id": row["id"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
    }


def consume_admin_invite(raw_token: str, telegram_user_id: int) -> dict:
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    with db() as conn:
        invite = conn.execute(
            """
            SELECT id
            FROM admin_invites
            WHERE token_hash=%s
              AND used_at IS NULL
              AND expires_at > NOW()
            FOR UPDATE
            """,
            (token_hash,),
        ).fetchone()

        if not invite:
            return {"ok": False, "reason": "invalid_or_expired"}

        user = conn.execute(
            """
            SELECT role
            FROM app_users
            WHERE telegram_user_id=%s
            FOR UPDATE
            """,
            (telegram_user_id,),
        ).fetchone()

        if not user:
            return {"ok": False, "reason": "user_not_found"}

        new_role = "creator" if user["role"] == "creator" else "admin"

        conn.execute(
            """
            UPDATE app_users
            SET role=%s, updated_at=NOW(), last_seen_at=NOW()
            WHERE telegram_user_id=%s
            """,
            (new_role, telegram_user_id),
        )

        conn.execute(
            """
            UPDATE admin_invites
            SET used_at=NOW(), used_by=%s
            WHERE id=%s
            """,
            (telegram_user_id, invite["id"]),
        )

    return {"ok": True, "role": new_role}



def grant_admin(telegram_user_id: int) -> dict:
    """Persistently grant admin role by Telegram user ID."""
    creator_id = get_creator_telegram_id()
    if creator_id == telegram_user_id:
        return {"ok": False, "reason": "creator"}

    with db() as conn:
        row = conn.execute(
            """
            INSERT INTO app_users (telegram_user_id, role)
            VALUES (%s, 'admin')
            ON CONFLICT (telegram_user_id)
            DO UPDATE SET role='admin', updated_at=NOW()
            RETURNING telegram_user_id, first_name, last_name, username, role,
                      created_at, updated_at, last_seen_at
            """,
            (telegram_user_id,),
        ).fetchone()

    return {"ok": True, "user": dict(row)}


def list_role_users() -> list[dict]:
    """Return known users for synchronising their personal Telegram menu button."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT telegram_user_id, role
            FROM app_users
            ORDER BY telegram_user_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def list_admins() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT telegram_user_id, first_name, last_name, username, role,
                   created_at, updated_at, last_seen_at
            FROM app_users
            WHERE role='admin'
            ORDER BY first_name NULLS LAST, telegram_user_id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_admin(telegram_user_id: int) -> bool:
    creator_id = get_creator_telegram_id()
    if creator_id == telegram_user_id:
        return False

    with db() as conn:
        row = conn.execute(
            """
            UPDATE app_users
            SET role='client', updated_at=NOW()
            WHERE telegram_user_id=%s AND role='admin'
            RETURNING telegram_user_id
            """,
            (telegram_user_id,),
        ).fetchone()
    return bool(row)

from __future__ import annotations

import hashlib
import json
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
                client_name TEXT,
                phone TEXT,
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
            "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS client_name TEXT"
        )
        conn.execute(
            "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS phone TEXT"
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
            CREATE TABLE IF NOT EXISTS admin_access_requests (
                id BIGSERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                decided_at TIMESTAMPTZ,
                decided_by BIGINT,
                CONSTRAINT admin_access_requests_status_check
                    CHECK (status IN ('pending', 'approved', 'rejected'))
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_admin_access_requests_status_created
            ON admin_access_requests (status, created_at DESC)
            """
        )


        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beauty_services (
                id BIGSERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                price INTEGER,
                duration INTEGER,
                description TEXT,
                includes JSONB NOT NULL DEFAULT '[]'::jsonb,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                deleted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_beauty_services_catalog
            ON beauty_services (is_active, deleted_at, sort_order, id)
            """
        )


        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (1, 'Косметология', 'Чистка лица механическая', 15000, 30, 'Механическая чистка лица — глубокое очищение кожи от загрязнений и комедонов вручную с использованием профессиональных инструментов.', '["Консультация", "Очищение кожи", "Механическая чистка", "Завершающий уход", "Рекомендации по уходу"]', True, 1),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (2, 'Пирсинг', 'Детский прокол ушей', 4000, 20, 'Аккуратный прокол мочек ушей для детей с внимательным и спокойным подходом к процедуре.', '["Подбор места прокола", "Подготовка и обработка зоны", "Прокол мочек ушей", "Рекомендации по домашнему уходу"]', True, 2),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (3, 'Косметология', 'Удаление кожных новообразований', 25000, 40, 'Удаление подходящих кожных новообразований после предварительной оценки специалистом.', '["Предварительная консультация", "Оценка зоны", "Проведение процедуры при отсутствии противопоказаний", "Рекомендации по уходу после процедуры"]', True, 3),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (4, 'Пирсинг', 'Пирсинг мочки', 6000, 20, 'Прокол мочки — классический и аккуратный прокол мягкой ткани уха с подбором подходящего украшения.', '["Консультация", "Разметка", "Прокол", "Установка украшения", "Рекомендации по уходу"]', True, 4),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (5, 'Пирсинг', 'Пирсинг ноздри (Nostril)', 5000, 15, 'Пирсинг ноздри — стильный акцент на лице с подбором украшения и профессиональным выполнением прокола.', '["Консультация", "Подбор украшения", "Проведение процедуры", "Рекомендации по уходу"]', True, 5),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (6, 'Пирсинг', 'Вертикальный лабрет', 8000, 20, 'Вертикальный лабрет — стильный и аккуратный прокол нижней губы. Подбор украшения с учётом анатомии и профессиональное выполнение процедуры.', '[]', True, 6),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (7, 'Пирсинг', 'Пирсинг языка', 7000, 15, 'Классический прокол языка и отдельные варианты поверхностного пирсинга после оценки анатомии.', '["Классический прокол языка", "Оценка анатомии перед процедурой", "Некоторые поверхностные варианты", "Подробные рекомендации по уходу"]', True, 7),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (8, 'Пирсинг', 'Пирсинг брови', 16000, 20, 'Стандартный прокол брови с выбором подходящего расположения и украшения.', '["Подбор точки прокола", "Стандартный прокол брови", "Установка украшения", "Рекомендации по уходу"]', True, 8),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (11, 'Аппаратные процедуры', 'Лазерная эпиляция', 18000, 30, 'Аппаратная процедура для длительного уменьшения роста нежелательных волос на выбранных зонах.', '["Подбор зоны обработки", "Настройка параметров под клиента", "Проведение процедуры", "Рекомендации между сеансами"]', True, 11),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (12, 'Аппаратные процедуры', 'Курс аппаратной коррекции', 30000, 40, 'Курс процедур для работы с выбранными зонами тела по индивидуально подобранной программе.', '["Первичная консультация", "Подбор курса", "Аппаратная работа по выбранным зонам", "Контроль динамики в процессе курса"]', True, 12),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (13, 'Аппаратные процедуры', 'Токовая терапия', 30000, 30, 'Аппаратная процедура с использованием слабых электрических импульсов по индивидуально выбранному протоколу.', '["Оценка состояния кожи", "Подбор режима процедуры", "Проведение токовой терапии", "Рекомендации по дальнейшему уходу"]', True, 13),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (14, 'Сертификаты', 'Подарочные сертификаты', None, None, 'Красивый способ подарить близкому человеку процедуру или возможность самостоятельно выбрать услугу.', '["Подбор номинала", "Возможность подарить на выбранную услугу", "Оформление сертификата", "Уточнение условий использования при записи"]', True, 14),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (15, 'Пирсинг', 'Пирсинг пупка', 10000, 15, 'Пирсинг пупка — аккуратное украшение тела, подчёркивающее линию живота. Индивидуальный подбор украшения и профессиональное выполнение процедуры.', '["Консультация", "Проведение услуги", "Рекомендации по уходу"]', True, 15),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (16, 'Косметология', 'Чистка лица аппаратная', 30000, 30, 'Аппаратная чистка лица — бережное очищение кожи с помощью профессионального косметологического аппарата, удаление загрязнений и улучшение состояния кожи.', '["Консультация", "Очищение кожи", "Аппаратная чистка", "Завершающий уход", "Рекомендации по уходу"]', True, 16),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (17, 'Пирсинг', 'Пирсинг сосков', 25000, 20, 'Пирсинг сосков — стильный интимный прокол с подбором украшения с учётом анатомии и профессиональным выполнением процедуры.', '["Консультация", "Разметка", "Прокол", "Установка украшения", "Рекомендации по уходу"]', True, 17),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (18, 'Пирсинг', 'Индустриал', 16000, 15, 'Индустриал — эффектный прокол хряща уха с установкой одной прямой штанги, соединяющей два прокола.', '["Консультация", "Разметка", "Прокол", "Установка украшения", "Рекомендации по уходу"]', True, 18),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (19, 'Пирсинг', 'Септум', 15000, 15, 'Септум — стильный прокол носовой перегородки с установкой аккуратного кольца или другого подходящего украшения.', '["Консультация", "Разметка", "Прокол", "Установка украшения", "Рекомендации по уходу"]', True, 19),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (20, 'Пирсинг', 'Бридж', 10000, 20, 'Бридж — горизонтальный прокол кожи в области переносицы между глазами с установкой аккуратного украшения.', '["Консультация", "Разметка", "Прокол", "Установка украшения", "Рекомендации по уходу"]', True, 20),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (21, 'Пирсинг', 'Лабрет', 8000, 10, 'Лабрет — классический прокол под нижней губой с установкой аккуратного украшения.', '["Консультация", "Разметка", "Прокол", "Установка украшения", "Рекомендации по уходу"]', True, 21),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (22, 'Пирсинг', 'Медуза', 8000, 15, 'Медуза — стильный прокол по центру над верхней губой, в области фильтрума, с установкой аккуратного украшения.', '["Консультация", "Разметка", "Прокол", "Установка украшения", "Рекомендации по уходу"]', True, 22),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (23, 'Пирсинг', 'Монро', 10000, 15, 'Монро — изящный прокол над верхней губой сбоку, имитирующий аккуратную «родинку».', '["Консультация", "Разметка", "Прокол", "Установка украшения", "Рекомендации по уходу"]', True, 23),
        )

        conn.execute(
            """
            INSERT INTO beauty_services (
                id, category, name, price, duration,
                description, includes, is_active, sort_order
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (id) DO NOTHING
            """,
            (24, 'Пирсинг', 'Трагус', 15000, 20, 'Трагус — аккуратный прокол небольшого хрящевого выступа перед слуховым проходом.', '["Консультация", "Разметка", "Прокол", "Установка украшения", "Рекомендации по уходу"]', True, 24),
        )

        conn.execute(
            """
            SELECT setval(
                pg_get_serial_sequence('beauty_services','id'),
                GREATEST(COALESCE((SELECT MAX(id) FROM beauty_services), 1), 1),
                true
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beauty_bookings (
                id BIGSERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                first_name TEXT,
                username TEXT,
                client_name TEXT,
                phone TEXT,
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
            "ALTER TABLE beauty_bookings ADD COLUMN IF NOT EXISTS client_name TEXT"
        )
        conn.execute(
            "ALTER TABLE beauty_bookings ADD COLUMN IF NOT EXISTS phone TEXT"
        )
        # Manual bookings may come from phone / Instagram and therefore do not
        # necessarily have a Telegram account linked to them.
        conn.execute(
            "ALTER TABLE beauty_bookings ALTER COLUMN telegram_user_id DROP NOT NULL"
        )
        conn.execute(
            "ALTER TABLE beauty_bookings ADD COLUMN IF NOT EXISTS note TEXT"
        )
        conn.execute(
            "ALTER TABLE beauty_bookings ADD COLUMN IF NOT EXISTS booking_source TEXT NOT NULL DEFAULT 'client'"
        )
        conn.execute(
            "ALTER TABLE beauty_bookings ADD COLUMN IF NOT EXISTS price_snapshot INTEGER"
        )
        conn.execute(
            "ALTER TABLE beauty_bookings ADD COLUMN IF NOT EXISTS duration_snapshot INTEGER"
        )
        conn.execute(
            """
            UPDATE beauty_bookings b
            SET price_snapshot=s.price
            FROM beauty_services s
            WHERE b.price_snapshot IS NULL AND s.id=b.service_id
            """
        )
        conn.execute(
            """
            UPDATE beauty_bookings b
            SET duration_snapshot=COALESCE(s.duration, 60)
            FROM beauty_services s
            WHERE b.duration_snapshot IS NULL AND s.id=b.service_id
            """
        )
        conn.execute(
            """
            UPDATE beauty_bookings
            SET duration_snapshot=60
            WHERE duration_snapshot IS NULL
            """
        )

        conn.execute(
            """
            UPDATE app_users AS u
            SET client_name = COALESCE(u.client_name, latest.client_name),
                phone = COALESCE(u.phone, latest.phone),
                updated_at = CASE
                    WHEN u.client_name IS NULL OR u.phone IS NULL THEN NOW()
                    ELSE u.updated_at
                END
            FROM (
                SELECT DISTINCT ON (telegram_user_id)
                       telegram_user_id, client_name, phone
                FROM beauty_bookings
                WHERE telegram_user_id IS NOT NULL
                ORDER BY telegram_user_id, created_at DESC, id DESC
            ) AS latest
            WHERE u.telegram_user_id = latest.telegram_user_id
              AND (u.client_name IS NULL OR u.phone IS NULL)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beauty_blocked_slots (
                id BIGSERIAL PRIMARY KEY,
                master_id INTEGER NOT NULL DEFAULT 1,
                booking_date DATE NOT NULL,
                booking_time TIME NOT NULL,
                label TEXT,
                created_by BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_beauty_blocked_slot
            ON beauty_blocked_slots (master_id, booking_date, booking_time)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_beauty_blocked_slot_date
            ON beauty_blocked_slots (booking_date, booking_time)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beauty_client_notes (
                client_key TEXT PRIMARY KEY,
                note TEXT NOT NULL DEFAULT '',
                updated_by BIGINT,
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

        # Gift certificates are a separate business module.  A certificate has
        # exactly one current owner and one live QR token.  The QR token is
        # rotated whenever ownership changes, so screenshots made by a previous
        # owner stop working immediately after the gift is accepted.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beauty_certificates (
                id BIGSERIAL PRIMARY KEY,
                owner_telegram_user_id BIGINT,
                purchased_by_telegram_user_id BIGINT,
                title TEXT NOT NULL DEFAULT 'Подарочный сертификат',
                amount INTEGER,
                service_name TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                qr_token TEXT NOT NULL UNIQUE,
                issued_by BIGINT,
                claim_token_hash TEXT,
                claim_expires_at TIMESTAMPTZ,
                claimed_at TIMESTAMPTZ,
                expires_at DATE,
                used_at TIMESTAMPTZ,
                used_by BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT beauty_certificates_status_check
                    CHECK (status IN ('active', 'used', 'cancelled')),
                CONSTRAINT beauty_certificates_amount_check
                    CHECK (amount IS NULL OR amount >= 0)
            )
            """
        )
        # Existing databases may have been created by the first certificate MVP.
        # Keep this migration idempotent and allow certificates that wait for a
        # completely new Telegram user to claim them via a one-time link.
        conn.execute("ALTER TABLE beauty_certificates ALTER COLUMN owner_telegram_user_id DROP NOT NULL")
        conn.execute("ALTER TABLE beauty_certificates ALTER COLUMN purchased_by_telegram_user_id DROP NOT NULL")
        conn.execute("ALTER TABLE beauty_certificates ADD COLUMN IF NOT EXISTS claim_token_hash TEXT")
        conn.execute("ALTER TABLE beauty_certificates ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE beauty_certificates ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_beauty_certificates_claim_token
            ON beauty_certificates (claim_token_hash)
            WHERE claim_token_hash IS NOT NULL
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_beauty_certificates_owner
            ON beauty_certificates (owner_telegram_user_id, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_beauty_certificates_status
            ON beauty_certificates (status, expires_at, created_at DESC)
            """
        )

        # Transfer links contain a random raw token in Telegram, while only its
        # SHA-256 hash is persisted.  One certificate can have only one pending
        # transfer at a time; creating a new link cancels the previous one.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beauty_certificate_transfers (
                id BIGSERIAL PRIMARY KEY,
                certificate_id BIGINT NOT NULL REFERENCES beauty_certificates(id) ON DELETE CASCADE,
                from_telegram_user_id BIGINT NOT NULL,
                to_telegram_user_id BIGINT,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TIMESTAMPTZ NOT NULL,
                accepted_at TIMESTAMPTZ,
                cancelled_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_beauty_certificate_pending_transfer
            ON beauty_certificate_transfers (certificate_id)
            WHERE accepted_at IS NULL AND cancelled_at IS NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_beauty_certificate_transfers_from
            ON beauty_certificate_transfers (from_telegram_user_id, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_beauty_certificate_transfers_to
            ON beauty_certificate_transfers (to_telegram_user_id, accepted_at DESC)
            """
        )

        # Idempotency ledger for automatic Telegram reminders.  The event key
        # includes the current booking date/time, so a rescheduled booking can
        # legitimately receive a fresh set of reminders.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS beauty_reminder_log (
                event_key TEXT PRIMARY KEY,
                reminder_type TEXT NOT NULL,
                booking_id BIGINT,
                recipient_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                sent_at TIMESTAMPTZ
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_beauty_reminder_log_booking
            ON beauty_reminder_log (booking_id, reminder_type)
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
            SELECT telegram_user_id, first_name, last_name, username, client_name, phone, role,
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
                RETURNING telegram_user_id, first_name, last_name, username, client_name, phone,
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
            RETURNING telegram_user_id, first_name, last_name, username, client_name, phone,
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
            SELECT telegram_user_id, first_name, last_name, username, client_name, phone, role,
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


def create_admin_access_request(user: dict) -> dict:
    """Create a new pending request. Intentionally no anti-spam/deduplication."""
    telegram_user_id = int(user["telegram_user_id"])
    with db() as conn:
        row = conn.execute(
            """
            INSERT INTO admin_access_requests (
                telegram_user_id, first_name, last_name, username, status
            )
            VALUES (%s, %s, %s, %s, 'pending')
            RETURNING id, telegram_user_id, first_name, last_name, username,
                      status, created_at
            """,
            (
                telegram_user_id,
                user.get("first_name"),
                user.get("last_name"),
                user.get("username"),
            ),
        ).fetchone()
    return dict(row)


def list_admin_access_requests(status: str = "pending") -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, telegram_user_id, first_name, last_name, username,
                   status, created_at, decided_at, decided_by
            FROM admin_access_requests
            WHERE status=%s
            ORDER BY created_at DESC, id DESC
            """,
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


def decide_admin_access_request(request_id: int, decision: str, decided_by: int) -> dict:
    if decision not in {"approved", "rejected"}:
        return {"ok": False, "reason": "invalid_decision"}

    with db() as conn:
        request = conn.execute(
            """
            SELECT id, telegram_user_id, first_name, last_name, username, status
            FROM admin_access_requests
            WHERE id=%s
            FOR UPDATE
            """,
            (request_id,),
        ).fetchone()

        if not request:
            return {"ok": False, "reason": "not_found"}
        if request["status"] != "pending":
            return {"ok": False, "reason": "already_decided"}

        telegram_user_id = int(request["telegram_user_id"])

        if decision == "approved":
            creator_id = get_creator_telegram_id()
            if creator_id == telegram_user_id:
                return {"ok": False, "reason": "creator"}

            conn.execute(
                """
                UPDATE app_users
                SET role='admin', updated_at=NOW(), last_seen_at=NOW()
                WHERE telegram_user_id=%s
                """,
                (telegram_user_id,),
            )

        row = conn.execute(
            """
            UPDATE admin_access_requests
            SET status=%s, decided_at=NOW(), decided_by=%s
            WHERE id=%s
            RETURNING id, telegram_user_id, first_name, last_name, username,
                      status, created_at, decided_at, decided_by
            """,
            (decision, decided_by, request_id),
        ).fetchone()

    return {"ok": True, "request": dict(row)}

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
import threading
from datetime import date, datetime, time as dt_time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from database import consume_admin_invite, create_admin_access_request, db, get_app_user, list_role_users, upsert_app_user

router = APIRouter()


def _bot_token() -> str:
    return os.getenv("BOT_TOKEN", "").strip()


def _base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


def bot_webhook_enabled() -> bool:
    return os.getenv("BOT_WEBHOOK_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _webhook_secret() -> str:
    token = _bot_token()
    return hashlib.sha256(
        (token + "|med-aesthetic-webhook").encode("utf-8")
    ).hexdigest()


def telegram_api(method: str, payload: dict | None = None) -> dict:
    token = _bot_token()
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured")

    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(
            f"Telegram API {method} failed: HTTP {exc.code}: {detail}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"Telegram API {method} failed: {exc}"
        ) from exc

    if not data.get("ok"):
        raise RuntimeError(
            f"Telegram API {method} failed: {data.get('description', 'unknown error')}"
        )
    return data



def _app_url_for_role(role: str) -> str:
    base_url = _base_url()
    if not base_url:
        return ""
    entry = role if role in {"creator", "admin", "client"} else "client"
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}entry={entry}"


def set_role_menu_button(chat_id: int, role: str) -> None:
    """Set a personal Mini App menu button with an authoritative startup role hint."""
    url = _app_url_for_role(role)
    if not url:
        return

    label = {
        "creator": "Открыть приложение",
        "admin": "Открыть приложение",
        "client": "Открыть приложение",
    }.get(role, "Открыть приложение")

    telegram_api(
        "setChatMenuButton",
        {
            "chat_id": int(chat_id),
            "menu_button": {
                "type": "web_app",
                "text": label,
                "web_app": {"url": url},
            },
        },
    )


def safe_set_role_menu_button(chat_id: int, role: str) -> None:
    try:
        set_role_menu_button(int(chat_id), role)
    except Exception as exc:
        print(f"[telegram] menu button update for {chat_id} failed: {exc}")


def sync_role_menu_buttons() -> None:
    """Repair stale menu buttons after deploy/restart."""
    try:
        users = list_role_users()
    except Exception as exc:
        print(f"[telegram] role menu sync skipped: {exc}")
        return

    for user in users:
        safe_set_role_menu_button(
            int(user["telegram_user_id"]),
            str(user.get("role") or "client"),
        )


@lru_cache(maxsize=1)
def get_bot_username() -> str:
    data = telegram_api("getMe")
    return str(data["result"]["username"])


def make_admin_invite_link(raw_token: str) -> str:
    username = get_bot_username()
    return f"https://t.me/{username}?start=admin_{raw_token}"


def make_certificate_gift_link(raw_token: str) -> str:
    """Create a Telegram deep link that can be forwarded to the gift recipient."""
    username = get_bot_username()
    return f"https://t.me/{username}?start=gift_{raw_token}"


def _send_certificate_gift_message(chat_id: int, raw_token: str) -> None:
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with db() as conn:
        row = conn.execute(
            """
            SELECT c.id, c.title, c.amount, c.service_name, c.status, c.expires_at
            FROM beauty_certificate_transfers t
            JOIN beauty_certificates c ON c.id=t.certificate_id
            WHERE t.token_hash=%s
              AND t.accepted_at IS NULL
              AND t.cancelled_at IS NULL
              AND t.expires_at>NOW()
            """,
            (token_hash,),
        ).fetchone()

    if not row:
        _send_message(
            chat_id,
            "Эта ссылка на подарок недействительна или уже использована.",
            with_app_button=False,
        )
        return

    if str(row.get("status") or "active") != "active":
        _send_message(
            chat_id,
            "Этот сертификат уже недействителен.",
            with_app_button=False,
        )
        return

    if row.get("expires_at") and row["expires_at"] < date.today():
        _send_message(
            chat_id,
            "Срок действия этого сертификата уже истёк.",
            with_app_button=False,
        )
        return

    app_url = _app_url_for_role("client")
    if not app_url:
        _send_message(chat_id, "Подарок найден, но Mini App временно недоступен.", with_app_button=False)
        return
    app_url += f"&gift={raw_token}"

    detail = row.get("service_name") or (f"{int(row['amount']):,} ₸".replace(",", " ") if row.get("amount") is not None else "Подарочный сертификат")
    telegram_api(
        "sendMessage",
        {
            "chat_id": int(chat_id),
            "text": f"🎁 Вам подарили сертификат MED AESTHETIC\n\n{row.get('title') or 'Подарочный сертификат'}\n{detail}\n\nОткройте подарок и примите его в приложении.",
            "reply_markup": {
                "inline_keyboard": [[{
                    "text": "Открыть подарок",
                    "web_app": {"url": app_url},
                }]]
            },
        },
    )


def _send_message(chat_id: int, text: str, with_app_button: bool = True) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
    }

    base_url = _base_url()
    if with_app_button and base_url:
        role = "client"
        try:
            app_user = get_app_user(int(chat_id))
            if app_user and app_user.get("role") in {"creator", "admin", "client"}:
                role = str(app_user["role"])
        except Exception:
            pass
        app_url = _app_url_for_role(role) or base_url
        payload["reply_markup"] = {
            "inline_keyboard": [
                [
                    {
                        "text": "Открыть приложение",
                        "web_app": {"url": app_url},
                    }
                ]
            ]
        }

    telegram_api("sendMessage", payload)


def send_notification(chat_id: int, text: str) -> None:
    """Send an important app notification without breaking the API on Telegram errors."""
    try:
        _send_message(int(chat_id), text, with_app_button=True)
    except Exception as exc:
        print(f"[telegram] notification to {chat_id} failed: {exc}")


def send_notifications(chat_ids: list[int], text: str) -> None:
    """Send the same notification to unique Telegram users."""
    seen: set[int] = set()
    for raw_id in chat_ids:
        try:
            chat_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if chat_id in seen:
            continue
        seen.add(chat_id)
        send_notification(chat_id, text)



# ---------------------------------------------------------------------------
# Automatic booking reminders
# ---------------------------------------------------------------------------
_REMINDER_THREAD: threading.Thread | None = None
_REMINDER_STOP = threading.Event()


def _app_timezone() -> ZoneInfo:
    name = os.getenv("APP_TIMEZONE", "Asia/Almaty").strip() or "Asia/Almaty"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Almaty")


def _booking_datetime(row: dict) -> datetime:
    return datetime.combine(row["booking_date"], row["booking_time"]).replace(tzinfo=_app_timezone())


def _reminder_client_text(row: dict, kind: str, *, test: bool = False) -> str:
    prefix = "🧪 ТЕСТ · клиентское напоминание\n\n" if test else ""
    if kind == "client_24h":
        title = "Напоминаем о вашей записи завтра"
    else:
        title = "Ждём вас сегодня"
    return (
        f"{prefix}MED AESTHETIC\n\n"
        f"{title}\n"
        f"{row['service_name']}\n"
        f"{row['booking_date'].strftime('%d.%m.%Y')} в {row['booking_time'].strftime('%H:%M')}\n"
        f"Мастер: {row.get('master_name') or 'Людмила'}"
    )


def _reminder_admin_text(row: dict, *, test: bool = False) -> str:
    prefix = "🧪 ТЕСТ · напоминание администратору\n\n" if test else ""
    client = row.get("client_name") or row.get("first_name") or (f"@{row['username']}" if row.get("username") else "Клиент")
    phone = row.get("phone") or "не указан"
    return (
        f"{prefix}⏰ Запись через 1 час\n\n"
        f"Клиент: {client}\n"
        f"Услуга: {row['service_name']}\n"
        f"Время: {row['booking_time'].strftime('%H:%M')}\n"
        f"Телефон: {phone}\n"
        f"Запись #{row['id']}"
    )


def _today_admin_summary_text(rows: list[dict], *, test: bool = False) -> str:
    prefix = "🧪 ТЕСТ · утренняя сводка\n\n" if test else ""
    if not rows:
        return f"{prefix}MED AESTHETIC\n\nНа сегодня подтверждённых записей нет."
    lines = [f"{prefix}MED AESTHETIC", "", f"Записи на сегодня: {len(rows)}", ""]
    for row in rows:
        client = row.get("client_name") or row.get("first_name") or (f"@{row['username']}" if row.get("username") else "Клиент")
        lines.append(f"{row['booking_time'].strftime('%H:%M')} · {client} · {row['service_name']}")
    return "\n".join(lines)


def _claim_reminder(event_key: str, reminder_type: str, recipient_id: int, booking_id: int | None = None) -> bool:
    with db() as conn:
        row = conn.execute(
            """
            INSERT INTO beauty_reminder_log(event_key, reminder_type, booking_id, recipient_id)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (event_key) DO NOTHING
            RETURNING event_key
            """,
            (event_key, reminder_type, booking_id, int(recipient_id)),
        ).fetchone()
    return bool(row)


def _finish_reminder(event_key: str) -> None:
    with db() as conn:
        conn.execute("UPDATE beauty_reminder_log SET sent_at=NOW() WHERE event_key=%s", (event_key,))


def _release_reminder(event_key: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM beauty_reminder_log WHERE event_key=%s AND sent_at IS NULL", (event_key,))


def _send_logged_reminder(event_key: str, reminder_type: str, recipient_id: int, text: str, booking_id: int | None = None) -> None:
    if not _claim_reminder(event_key, reminder_type, recipient_id, booking_id):
        return
    try:
        _send_message(int(recipient_id), text, with_app_button=True)
        _finish_reminder(event_key)
    except Exception:
        _release_reminder(event_key)
        raise


def _confirmed_booking_rows(start_date: date, end_date: date) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, telegram_user_id, first_name, username, client_name, phone,
                   service_name, master_name, booking_date, booking_time, status
            FROM beauty_bookings
            WHERE status='confirmed'
              AND booking_date BETWEEN %s AND %s
            ORDER BY booking_date, booking_time, id
            """,
            (start_date, end_date),
        ).fetchall()
    return [dict(r) for r in rows]


def _staff_ids() -> list[int]:
    return [int(x["telegram_user_id"]) for x in list_role_users() if str(x.get("role")) in {"creator", "admin"}]


def run_due_reminders_once(now: datetime | None = None) -> None:
    tz = _app_timezone()
    now = (now or datetime.now(tz)).astimezone(tz)
    rows = _confirmed_booking_rows(now.date(), (now + timedelta(days=2)).date())
    staff_ids = _staff_ids()

    for row in rows:
        booking_at = _booking_datetime(row)
        seconds = (booking_at - now).total_seconds()
        if seconds <= 0:
            continue
        stamp = f"{row['booking_date'].isoformat()}-{row['booking_time'].strftime('%H%M')}"
        client_id = row.get("telegram_user_id")

        # Client: one reminder in the 24h→2h window, then one in the final 2h.
        if client_id and 2 * 3600 < seconds <= 24 * 3600:
            key = f"booking:{row['id']}:{stamp}:client24:{int(client_id)}"
            _send_logged_reminder(key, "client_24h", int(client_id), _reminder_client_text(row, "client_24h"), int(row["id"]))
        elif client_id and 0 < seconds <= 2 * 3600:
            key = f"booking:{row['id']}:{stamp}:client2:{int(client_id)}"
            _send_logged_reminder(key, "client_2h", int(client_id), _reminder_client_text(row, "client_2h"), int(row["id"]))

        # Staff: one hour before the booking.
        if 0 < seconds <= 3600:
            for staff_id in staff_ids:
                key = f"booking:{row['id']}:{stamp}:admin1:{staff_id}"
                _send_logged_reminder(key, "admin_1h", staff_id, _reminder_admin_text(row), int(row["id"]))

    # Daily summary, once per staff member after 08:00 local time.
    if now.time() >= dt_time(8, 0):
        today_rows = [r for r in rows if r["booking_date"] == now.date()]
        if today_rows:
            for staff_id in staff_ids:
                key = f"daily:{now.date().isoformat()}:summary:{staff_id}"
                _send_logged_reminder(key, "admin_daily", staff_id, _today_admin_summary_text(today_rows))


def _reminder_worker_loop() -> None:
    # Small initial delay lets startup/database migrations finish first.
    if _REMINDER_STOP.wait(5):
        return
    while not _REMINDER_STOP.is_set():
        try:
            run_due_reminders_once()
        except Exception as exc:
            print(f"[reminders] cycle failed: {exc}")
        _REMINDER_STOP.wait(60)


def start_reminder_worker() -> None:
    global _REMINDER_THREAD
    if _REMINDER_THREAD and _REMINDER_THREAD.is_alive():
        return
    _REMINDER_STOP.clear()
    _REMINDER_THREAD = threading.Thread(target=_reminder_worker_loop, name="beauty-reminders", daemon=True)
    _REMINDER_THREAD.start()
    print("[reminders] worker started")


def send_test_reminder(kind: str, booking_id: int | None, creator_chat_id: int) -> dict:
    allowed = {"client_24h", "client_2h", "admin_1h", "admin_daily"}
    if kind not in allowed:
        raise ValueError("Неизвестный тип тестового напоминания")

    today = datetime.now(_app_timezone()).date()
    if kind == "admin_daily":
        rows = _confirmed_booking_rows(today, today)
        _send_message(int(creator_chat_id), _today_admin_summary_text(rows, test=True), with_app_button=True)
        return {"ok": True, "recipient": int(creator_chat_id), "kind": kind}

    if not booking_id:
        raise ValueError("Выберите запись для теста")
    with db() as conn:
        row = conn.execute(
            """
            SELECT id, telegram_user_id, first_name, username, client_name, phone,
                   service_name, master_name, booking_date, booking_time, status
            FROM beauty_bookings WHERE id=%s
            """,
            (int(booking_id),),
        ).fetchone()
    if not row:
        raise ValueError("Запись не найдена")
    row = dict(row)

    if kind in {"client_24h", "client_2h"}:
        client_id = row.get("telegram_user_id")
        if not client_id:
            raise ValueError("У этой записи нет привязанного Telegram клиента")
        _send_message(int(client_id), _reminder_client_text(row, kind, test=True), with_app_button=True)
        return {"ok": True, "recipient": int(client_id), "kind": kind}

    _send_message(int(creator_chat_id), _reminder_admin_text(row, test=True), with_app_button=True)
    return {"ok": True, "recipient": int(creator_chat_id), "kind": kind}


def _admin_request_phrase() -> str:
    # Hidden phrase is not registered as a Telegram command.
    # Change ADMIN_REQUEST_PHRASE in server environment to replace it instantly.
    return os.getenv("ADMIN_REQUEST_PHRASE", "I am admin").strip()


def process_update(update: dict) -> None:
    try:
        message = update.get("message")
        if not message:
            return

        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text") or "").strip()

        if not sender.get("id") or not chat.get("id"):
            return

        telegram_user_id = int(sender["id"])
        chat_id = int(chat["id"])
        app_user = upsert_app_user(sender)
        safe_set_role_menu_button(
            telegram_user_id,
            str(app_user.get("role") or "client"),
        )

        admin_phrase = _admin_request_phrase()
        if admin_phrase and text == admin_phrase:
            if str(app_user.get("role") or "client") == "creator":
                _send_message(chat_id, "У вас уже есть права создателя MED AESTHETIC.")
                return
            if str(app_user.get("role") or "client") == "admin":
                _send_message(chat_id, "У вас уже есть права администратора MED AESTHETIC.")
                return

            create_admin_access_request(app_user)
            _send_message(
                chat_id,
                "Заявка на права администратора отправлена создателю MED AESTHETIC.",
                with_app_button=False,
            )
            return

        if text == "/id" or text.startswith("/id@"):
            _send_message(
                chat_id,
                f"Ваш Telegram ID: {telegram_user_id}",
                with_app_button=False,
            )
            return

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            payload = parts[1].strip() if len(parts) > 1 else ""

            if payload.startswith("gift_"):
                raw_token = payload[len("gift_"):].strip()
                if not raw_token:
                    _send_message(chat_id, "Некорректная ссылка на подарок.", with_app_button=False)
                    return
                _send_certificate_gift_message(chat_id, raw_token)
                return

            if payload.startswith("admin_"):
                raw_token = payload[len("admin_"):]
                result = consume_admin_invite(raw_token, telegram_user_id)

                if result.get("ok"):
                    safe_set_role_menu_button(
                        telegram_user_id,
                        str(result.get("role") or "admin"),
                    )
                    if result.get("role") == "creator":
                        msg = (
                            "Приглашение проверено. Вы остались создателем — "
                            "права создателя не понижаются."
                        )
                    else:
                        msg = (
                            "Готово. Права администратора MED AESTHETIC активированы."
                        )
                    _send_message(chat_id, msg)
                    return

                _send_message(
                    chat_id,
                    "Эта ссылка администратора недействительна или уже использована.",
                )
                return

            role = app_user.get("role", "client")
            role_text = {
                "creator": "создатель",
                "admin": "администратор",
                "client": "клиент",
            }.get(role, "клиент")

            _send_message(
                chat_id,
                f"MED AESTHETIC готово к работе.\nВаша роль: {role_text}.",
            )
            return
    except Exception as exc:
        print(f"[telegram] update processing error: {exc}")


def setup_telegram_webhook() -> None:
    if not bot_webhook_enabled():
        print("[telegram] webhook registration disabled")
        return

    token = _bot_token()
    base_url = _base_url()
    if not token:
        print("[telegram] BOT_TOKEN is missing; webhook not configured")
        return
    if not base_url:
        print("[telegram] PUBLIC_BASE_URL is missing; webhook not configured")
        return

    try:
        webhook_url = f"{base_url}/telegram/webhook"
        telegram_api(
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": _webhook_secret(),
                "allowed_updates": ["message"],
                "drop_pending_updates": False,
            },
        )
        telegram_api(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Открыть MED AESTHETIC"},
                    {"command": "id", "description": "Показать мой Telegram ID"},
                ]
            },
        )
        sync_role_menu_buttons()
        print(f"[telegram] webhook configured: {webhook_url}")
    except Exception as exc:
        print(f"[telegram] webhook setup failed: {exc}")


@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    if not bot_webhook_enabled():
        raise HTTPException(status_code=404, detail="Not found")

    supplied_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )
    if supplied_secret != _webhook_secret():
        raise HTTPException(status_code=403, detail="Forbidden")

    update = await request.json()
    background_tasks.add_task(process_update, update)
    return {"ok": True}

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from functools import lru_cache

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from database import consume_admin_invite, upsert_app_user

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


@lru_cache(maxsize=1)
def get_bot_username() -> str:
    data = telegram_api("getMe")
    return str(data["result"]["username"])


def make_admin_invite_link(raw_token: str) -> str:
    username = get_bot_username()
    return f"https://t.me/{username}?start=admin_{raw_token}"


def _send_message(chat_id: int, text: str, with_app_button: bool = True) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
    }

    base_url = _base_url()
    if with_app_button and base_url:
        payload["reply_markup"] = {
            "inline_keyboard": [
                [
                    {
                        "text": "Открыть MED AESTHETIC",
                        "web_app": {"url": base_url},
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

            if payload.startswith("admin_"):
                raw_token = payload[len("admin_"):]
                result = consume_admin_invite(raw_token, telegram_user_id)

                if result.get("ok"):
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

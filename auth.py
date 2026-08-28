from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException


def validate_telegram_init_data(init_data: str, max_age_seconds: int = 86400) -> dict:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="BOT_TOKEN is not configured")
    if not init_data:
        raise HTTPException(status_code=401, detail="Telegram authorization data not found")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram authorization hash not found")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(received_hash, expected_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram authorization data")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        auth_date = 0

    if auth_date <= 0 or time.time() - auth_date > max_age_seconds:
        raise HTTPException(status_code=401, detail="Telegram authorization data expired")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        user = {}

    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user not found")

    return user

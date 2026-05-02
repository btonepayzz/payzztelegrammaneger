"""
Eksik ortam değişkenlerini terminalden sorar, .env dosyasına kaydeder.
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from env_sanitize import (
    looks_like_bot_token,
    sanitize_api_hash,
    sanitize_api_id,
    sanitize_bot_token,
    sanitize_session_name,
)

_KEYS = (
    "BOT_TOKEN",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELETHON_SESSION_NAME",
    "SYNC_INTERVAL_SEC",
)

_REQUIRED = ("BOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH")


def _norm(v: str | None) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _norm_token(v: str | None) -> str:
    return sanitize_bot_token(_norm(v))


def _norm_hash(v: str | None) -> str:
    return sanitize_api_hash(_norm(v))


def _norm_id(v: str | None) -> str:
    return sanitize_api_id(_norm(v))


def _session_from_raw(raw_val: str | None) -> str:
    n = _norm(raw_val)
    if not n:
        return ""
    return sanitize_session_name(n)


def _needs_prompt(values: dict[str, str | None]) -> bool:
    tok = _norm_token(values.get("BOT_TOKEN"))
    aid = _norm_id(values.get("TELEGRAM_API_ID"))
    hsh = _norm_hash(values.get("TELEGRAM_API_HASH"))
    return not (tok and aid and hsh and looks_like_bot_token(tok))


def _gather_missing(env_path: Path) -> dict[str, str]:
    raw = dotenv_values(env_path) if env_path.exists() else {}
    merged: dict[str, str] = {
        "BOT_TOKEN": _norm_token(raw.get("BOT_TOKEN")),
        "TELEGRAM_API_ID": _norm_id(raw.get("TELEGRAM_API_ID")),
        "TELEGRAM_API_HASH": _norm_hash(raw.get("TELEGRAM_API_HASH")),
        "TELETHON_SESSION_NAME": _session_from_raw(raw.get("TELETHON_SESSION_NAME")),
        "SYNC_INTERVAL_SEC": _norm(raw.get("SYNC_INTERVAL_SEC")),
    }

    print("\n=== Telegram bot / Telethon ayarları ===\n")
    print("@BotFather’dan BOT_TOKEN; https://my.telegram.org adresinden API_ID ve API_HASH.\n")

    if not merged["BOT_TOKEN"] or not looks_like_bot_token(merged["BOT_TOKEN"]):
        t = getpass.getpass("BOT_TOKEN: ")
        merged["BOT_TOKEN"] = sanitize_bot_token(t)
        if not merged["BOT_TOKEN"]:
            raise SystemExit("BOT_TOKEN boş olamaz.")
        if not looks_like_bot_token(merged["BOT_TOKEN"]):
            raise SystemExit(
                "BOT_TOKEN biçimi hatalı (örnek: 123456789:AAH... yalnızca rakam, harf, _, - ve iki nokta)."
            )

    if not merged["TELEGRAM_API_ID"]:
        merged["TELEGRAM_API_ID"] = sanitize_api_id(input("TELEGRAM_API_ID: "))
        if not merged["TELEGRAM_API_ID"]:
            raise SystemExit("TELEGRAM_API_ID boş olamaz.")

    if not merged["TELEGRAM_API_HASH"]:
        merged["TELEGRAM_API_HASH"] = sanitize_api_hash(getpass.getpass("TELEGRAM_API_HASH: "))
        if not merged["TELEGRAM_API_HASH"]:
            raise SystemExit("TELEGRAM_API_HASH boş olamaz.")

    if not merged["TELETHON_SESSION_NAME"]:
        d = input("TELETHON_SESSION_NAME [user_session]: ").strip()
        merged["TELETHON_SESSION_NAME"] = sanitize_session_name(d) if d else "user_session"

    if not merged["SYNC_INTERVAL_SEC"]:
        merged["SYNC_INTERVAL_SEC"] = "45"
    try:
        int(merged["SYNC_INTERVAL_SEC"])
    except ValueError:
        raise SystemExit("SYNC_INTERVAL_SEC geçerli bir tam sayı olmalı (.env dosyasını düzelt).")

    return merged


def _write_env(env_path: Path, data: dict[str, str]) -> None:
    text = (
        "# Telegram Bot (@BotFather)\n"
        f"BOT_TOKEN={data['BOT_TOKEN']}\n"
        "\n"
        "# https://my.telegram.org — Telethon kullanıcı hesabı\n"
        f"TELEGRAM_API_ID={data['TELEGRAM_API_ID']}\n"
        f"TELEGRAM_API_HASH={data['TELEGRAM_API_HASH']}\n"
        "# İlk çalıştırmada telefon doğrulaması istenir; oturum dosyası oluşur\n"
        f"TELETHON_SESSION_NAME={data['TELETHON_SESSION_NAME']}\n"
        "\n"
        "# Ortak grup listesini kaç saniyede bir yenilesin (canlıya yakın)\n"
        f"SYNC_INTERVAL_SEC={data['SYNC_INTERVAL_SEC']}\n"
    )
    env_path.write_text(text, encoding="utf-8")


def ensure_env_interactive(root: Path) -> None:
    env_path = root / ".env"
    load_dotenv(env_path)
    existing = dotenv_values(env_path) if env_path.exists() else {}
    effective: dict[str, str | None] = dict(existing)
    for key in _KEYS:
        ev = os.environ.get(key)
        if ev is not None and str(ev).strip():
            effective[key] = str(ev).strip()
    if not _needs_prompt(effective):
        return

    data = _gather_missing(env_path)
    _write_env(env_path, data)
    load_dotenv(env_path, override=True)
    print(f"\nAyarlar kaydedildi: {env_path}\n")

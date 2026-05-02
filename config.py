import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")


def _bootstrap_env() -> None:
    from env_wizard import ensure_env_interactive

    ensure_env_interactive(_ROOT)
    load_dotenv(_ROOT / ".env", override=True)


_bootstrap_env()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    telethon_session: str
    sync_interval_sec: int
    admin_user_id: int | None
    internal_api_host: str
    internal_api_port: int
    internal_panel_token: str
    web_panel_enabled: bool
    web_panel_port: int
    web_panel_bind_host: str
    web_panel_user: str
    web_panel_password_plain: str | None
    web_panel_password_hash: str | None
    web_panel_totp_secret: str | None
    web_panel_session_secret: str
    mail_forwarder_enabled: bool
    mail_imap_host: str
    mail_imap_user: str
    mail_imap_password: str
    mail_forward_chat_id: int | None
    mail_poll_interval_sec: int


def _parse_int_optional(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def load_settings() -> Settings:
    from env_sanitize import (
        looks_like_bot_token,
        sanitize_api_hash,
        sanitize_api_id,
        sanitize_bot_token,
        sanitize_session_name,
    )

    token = sanitize_bot_token(os.environ.get("BOT_TOKEN", ""))
    api_id_raw = sanitize_api_id(os.environ.get("TELEGRAM_API_ID", ""))
    api_hash = sanitize_api_hash(os.environ.get("TELEGRAM_API_HASH", ""))
    session = sanitize_session_name(os.environ.get("TELETHON_SESSION_NAME", "") or "user_session")
    sync = int(os.environ.get("SYNC_INTERVAL_SEC", "45"))
    admin_raw = os.environ.get("ADMIN_USER_ID", "").strip()
    admin_user_id: int | None = int(admin_raw) if admin_raw.isdigit() else None

    internal_host = os.environ.get("INTERNAL_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    internal_port = int(os.environ.get("INTERNAL_API_PORT", "18765"))
    panel_token = os.environ.get("INTERNAL_PANEL_TOKEN", "").strip()
    web_enabled = os.environ.get("WEB_PANEL_ENABLED", "1").strip() not in ("0", "false", "False", "")
    _wp = os.environ.get("WEB_PANEL_PORT", "").strip()
    _pa = os.environ.get("PORT", "").strip()
    if _wp:
        web_port = int(_wp)
    elif _pa:
        web_port = int(_pa)
    else:
        web_port = 3080
    bind_raw = os.environ.get("WEB_PANEL_BIND_HOST", "").strip()
    if bind_raw:
        web_bind = bind_raw
    elif os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"):
        web_bind = "0.0.0.0"
    else:
        web_bind = "127.0.0.1"
    wp_user = os.environ.get("WEB_PANEL_USER", "").strip()
    wp_pass_plain = os.environ.get("WEB_PANEL_PASSWORD", "").strip() or None
    wp_pass_hash = os.environ.get("WEB_PANEL_PASSWORD_HASH", "").strip() or None
    wp_totp = os.environ.get("WEB_PANEL_TOTP_SECRET", "").strip() or None
    sess_secret = os.environ.get("WEB_PANEL_SESSION_SECRET", "").strip()
    if not sess_secret and panel_token:
        sess_secret = hashlib.sha256(panel_token.encode()).hexdigest()
    elif not sess_secret:
        sess_secret = secrets.token_hex(32)

    if not token:
        raise ValueError("BOT_TOKEN eksik")
    if not looks_like_bot_token(token):
        raise ValueError(
            "BOT_TOKEN geçersiz görünüyor (yalnızca yazdırılabilir ASCII, 123456789:ABC... biçimi). "
            ".env içindeki tokenı kontrol et veya .env silip yeniden çalıştır."
        )
    if not api_id_raw or not api_hash:
        raise ValueError("TELEGRAM_API_ID / TELEGRAM_API_HASH eksik")

    mf_raw = os.environ.get("MAIL_FORWARDER_ENABLED", "0").strip().lower()
    mail_forwarder_enabled = mf_raw in ("1", "true", "yes", "on")
    mail_imap_host = os.environ.get("MAIL_IMAP_HOST", "").strip()
    mail_imap_user = os.environ.get("MAIL_IMAP_USER", "").strip()
    mail_imap_password = os.environ.get("MAIL_IMAP_PASSWORD", "").strip()
    mail_forward_chat_id = _parse_int_optional(os.environ.get("MAIL_FORWARD_CHAT_ID", ""))
    mail_poll_interval_sec = max(10, int(os.environ.get("MAIL_POLL_INTERVAL_SEC", "10")))

    return Settings(
        bot_token=token,
        api_id=int(api_id_raw),
        api_hash=api_hash,
        telethon_session=session,
        sync_interval_sec=max(15, sync),
        admin_user_id=admin_user_id,
        internal_api_host=internal_host,
        internal_api_port=internal_port,
        internal_panel_token=panel_token,
        web_panel_enabled=web_enabled and bool(panel_token),
        web_panel_port=web_port,
        web_panel_bind_host=web_bind,
        web_panel_user=wp_user,
        web_panel_password_plain=wp_pass_plain,
        web_panel_password_hash=wp_pass_hash,
        web_panel_totp_secret=wp_totp,
        web_panel_session_secret=sess_secret or "change-me-session-secret",
        mail_forwarder_enabled=mail_forwarder_enabled,
        mail_imap_host=mail_imap_host,
        mail_imap_user=mail_imap_user,
        mail_imap_password=mail_imap_password,
        mail_forward_chat_id=mail_forward_chat_id,
        mail_poll_interval_sec=mail_poll_interval_sec,
    )

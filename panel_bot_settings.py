"""
Grup yönetimi bot tokenı — panelden kayıt (panel_bot_settings.json).
Ortam değişkeni BOT_TOKEN üzerine yazılır (dosya varsa ve geçerliyse).
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def read_panel_bot_token(root: Path) -> str:
    """Dosyadan bot token; yoksa veya geçersizse boş string."""
    from env_sanitize import looks_like_bot_token, sanitize_bot_token

    p = root / "panel_bot_settings.json"
    if not p.is_file():
        return ""
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return ""
        t = sanitize_bot_token(str(raw.get("bot_token") or "").strip())
        if t and looks_like_bot_token(t):
            return t
    except Exception as e:
        log.warning("panel_bot_settings.json okunamadı: %s", e)
    return ""


def panel_bot_status(root: Path) -> dict[str, Any]:
    """GET /api/admin/tokens için özet (sırlar maskelenmiş)."""
    from env_sanitize import looks_like_bot_token, sanitize_bot_token

    p = root / "panel_bot_settings.json"
    has_file = p.is_file()
    file_token_ok = False
    if has_file:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                t = sanitize_bot_token(str(raw.get("bot_token") or "").strip())
                file_token_ok = bool(t and looks_like_bot_token(t))
        except Exception:
            pass

    env_raw = sanitize_bot_token(os.environ.get("BOT_TOKEN", "").strip())
    env_ok = bool(env_raw and looks_like_bot_token(env_raw))

    if file_token_ok:
        source = "panel_file"
    elif env_ok:
        source = "env"
    else:
        source = "none"

    return {
        "group_bot_token_configured": file_token_ok or env_ok,
        "group_bot_token_source": source,
        "panel_override_active": file_token_ok,
        "env_bot_token_present": env_ok,
    }


def write_panel_bot_token(root: Path, token: str) -> tuple[bool, str]:
    from env_sanitize import looks_like_bot_token, sanitize_bot_token

    t = sanitize_bot_token(token.strip())
    if not t or not looks_like_bot_token(t):
        return False, "Grup bot token geçersiz görünüyor"

    p = root / "panel_bot_settings.json"
    data: dict[str, Any] = {}
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = dict(raw)
        except Exception:
            pass
    data["bot_token"] = t

    tmp = p.parent / f".panel_bot_settings.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, str(e)

    log.info("panel_bot_settings.json güncellendi (grup bot token)")
    return True, ""


def revert_panel_bot_token(root: Path) -> tuple[bool, str]:
    """Dosyayı siler; bir sonraki süreç başlangıcında BOT_TOKEN ortam değişkeni kullanılır."""
    p = root / "panel_bot_settings.json"
    if not p.is_file():
        return True, ""
    try:
        p.unlink()
    except OSError as e:
        return False, str(e)
    log.info("panel_bot_settings.json kaldırıldı — grup bot için ortam BOT_TOKEN kullanılacak")
    return True, ""

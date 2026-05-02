"""
Davet paketi alıcıları — kalıcı kayıt (panelden iptal/silme için).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_lock = threading.Lock()


def _root() -> Path:
    return Path(__file__).resolve().parent


def _path() -> Path:
    return _root() / "invite_recipients.json"


def _load_raw() -> dict[str, Any]:
    p = _path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as e:
        log.warning("invite_recipients.json okunamadı: %s", e)
        return {}


def _save_raw(data: dict[str, Any]) -> None:
    p = _path()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = p.parent / f".invite_recipients.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def record_invite_package(
    username: str,
    target_id: int,
    operator_chat_id: int,
    *,
    telethon_dm_message_id: int | None,
) -> None:
    """Yeni davet paketi oluşturulduğunda (aynı hedef için önceki kayıt üzerine yazılır)."""
    key = str(int(target_id))
    with _lock:
        data = _load_raw()
        now = time.time()
        data[key] = {
            "username": username.strip().lstrip("@"),
            "target_id": int(target_id),
            "created_at": now,
            "operator_chat_id": int(operator_chat_id),
            "telethon_dm_message_id": telethon_dm_message_id,
            "bot_keyboard_message_ids": [],
            "keyboard_sent_at": None,
        }
        _save_raw(data)


def append_bot_keyboard_messages(target_id: int, message_ids: list[int]) -> None:
    if not message_ids:
        return
    key = str(int(target_id))
    with _lock:
        data = _load_raw()
        rec = data.get(key)
        if not isinstance(rec, dict):
            rec = {
                "username": "",
                "target_id": int(target_id),
                "created_at": time.time(),
                "operator_chat_id": 0,
                "telethon_dm_message_id": None,
                "bot_keyboard_message_ids": [],
                "keyboard_sent_at": None,
            }
        ids = list(rec.get("bot_keyboard_message_ids") or [])
        for mid in message_ids:
            try:
                ids.append(int(mid))
            except (TypeError, ValueError):
                pass
        rec["bot_keyboard_message_ids"] = ids
        rec["keyboard_sent_at"] = time.time()
        data[key] = rec
        _save_raw(data)


def list_recipients() -> list[dict[str, Any]]:
    with _lock:
        data = _load_raw()
    out: list[dict[str, Any]] = []
    for _k, rec in data.items():
        if not isinstance(rec, dict):
            continue
        tid = rec.get("target_id")
        if tid is None:
            continue
        out.append(
            {
                "username": rec.get("username") or "",
                "target_id": int(tid),
                "created_at": rec.get("created_at"),
                "keyboard_sent_at": rec.get("keyboard_sent_at"),
                "has_telethon_dm": rec.get("telethon_dm_message_id") is not None,
                "bot_message_count": len(rec.get("bot_keyboard_message_ids") or []),
            }
        )
    out.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
    return out


def pop_record(target_id: int) -> dict[str, Any] | None:
    key = str(int(target_id))
    with _lock:
        data = _load_raw()
        rec = data.pop(key, None)
        if isinstance(rec, dict):
            _save_raw(data)
            return rec
        return None



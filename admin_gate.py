"""Yönetici kontrolü: bot_data['admin_user_id'] ayarlıysa yalnızca bu kullanıcıya yanıt."""
from __future__ import annotations

from typing import Any

from telegram import Update


def is_operator(bot_data: dict[str, Any], user_id: int | None) -> bool:
    if user_id is None:
        return False
    aid = bot_data.get("admin_user_id")
    if aid is None:
        return True
    try:
        return int(user_id) == int(aid)
    except (TypeError, ValueError):
        return False


async def gate_operator_or_reply(update: Update, bot_data: dict[str, Any]) -> bool:
    """Yönetici değilse kısa uyarı döner; True = devam."""
    uid = update.effective_user.id if update.effective_user else None
    if is_operator(bot_data, uid):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Bu bot yalnızca yöneticiye açık.")
    return False

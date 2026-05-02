"""
Telegram Bot: grup üyeliği takibi + Telethon ile tamamlayıcı veri.
"""
from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from admin_gate import gate_operator_or_reply
from bulk_kick_flow import build_bulk_kick_conversation
from group_registry import GroupRegistry
from invite_flow import build_invite_conversation, build_invite_joined_callback
from telethon_service import TelethonService

log = logging.getLogger(__name__)


def _is_bot_still_member(status: str) -> bool:
    return status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.OWNER,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_operator_or_reply(update, context.bot_data):
        return
    await update.effective_message.reply_text(
        "Komutlar:\n"
        "/joint — Bot + hesabın birlikte olduğunuz gruplar\n"
        "/chat — Bu sohbetin bilgisi (bot + Telethon)\n"
        "/members — Bu grupta üye listesi (Telethon, limit 150)\n"
        "/members_id <chat_id> — ID ile üye listesi\n"
        "/refresh — Telethon + bot üyelik taraması (ortak gruplar)\n"
        "/toplucikar — Ortak gruplardan kullanıcı çıkarma (özel mesaj)\n"
        "/davet — Davet linklerini DM ile gönder (özel mesaj)"
    )


async def cmd_joint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_operator_or_reply(update, context.bot_data):
        return
    registry: GroupRegistry = context.bot_data["registry"]
    n = await registry.run_bot_membership_probe(context.bot, context.bot.id)
    snap = await registry.snapshot()
    if not snap.chat_ids:
        await update.effective_message.reply_text(
            "Ortak grup bulunamadı.\n"
            f"(Telethon sohbet sayısı tarandı, bot doğrulanan: {n})\n"
            "Kullanıcı hesabı ile botun aynı grupta olduğundan emin ol; "
            "bir süre sonra /refresh veya tekrar /joint dene."
        )
        return
    lines = []
    for cid in sorted(snap.chat_ids):
        title = snap.titles.get(cid) or "?"
        lines.append(f"• `{cid}` — {title}")
    text = "Birlikte olduğunuz gruplar:\n" + "\n".join(lines)
    await update.effective_message.reply_text(text, parse_mode="Markdown")


async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_operator_or_reply(update, context.bot_data):
        return
    chat = update.effective_chat
    if not chat or chat.id > 0:
        await update.effective_message.reply_text("Bu komut bir grupta kullanılmalı.")
        return
    tele: TelethonService = context.bot_data["telethon"]
    bot_info = f"id=`{chat.id}` title={chat.title!r}"
    extra = await tele.resolve_chat(chat.id)
    if extra.get("ok"):
        bot_info += (
            f"\nTelethon: title={extra.get('title')!r} "
            f"@{extra.get('username') or '-'} "
            f"üye≈{extra.get('participants_count')}"
        )
    else:
        bot_info += f"\nTelethon çözümleme: {extra.get('error')}"
    await update.effective_message.reply_text(bot_info, parse_mode="Markdown")


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_operator_or_reply(update, context.bot_data):
        return
    chat = update.effective_chat
    if not chat or chat.id > 0:
        await update.effective_message.reply_text("Bu komutu grupta kullan.")
        return
    await _send_members(update, context, chat.id)


async def cmd_members_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_operator_or_reply(update, context.bot_data):
        return
    if not context.args:
        await update.effective_message.reply_text("Kullanım: /members_id -100xxxxxxxxxx")
        return
    try:
        cid = int(context.args[0].strip())
    except ValueError:
        await update.effective_message.reply_text("Geçerli bir sayı gir.")
        return
    await _send_members(update, context, cid)


async def _send_members(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    tele: TelethonService = context.bot_data["telethon"]
    data = await tele.list_participants(chat_id, limit=150)
    if not data.get("ok"):
        await update.effective_message.reply_text(f"Üyeler alınamadı: {data.get('error')}")
        return
    members: list[dict[str, Any]] = data.get("members") or []
    header = f"{chat_id} — {len(members)} kayıt (limit 150)\n\n"
    max_body = 3900
    lines_body: list[str] = []
    for m in members:
        lines_body.append(f"{m['id']} @{m.get('username') or '-'} {m['name']}")
    blocks: list[str] = []
    cur = header
    for line in lines_body:
        piece = line + "\n"
        if len(cur) + len(piece) > max_body:
            blocks.append(cur.rstrip())
            cur = piece
        else:
            cur += piece
    if cur.strip():
        blocks.append(cur.rstrip())
    if not blocks:
        blocks.append(header + "(liste boş)")
    for i, block in enumerate(blocks):
        text = f"{chat_id} — devam\n\n{block}" if i else block
        await update.effective_message.reply_text(text)


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate_operator_or_reply(update, context.bot_data):
        return
    tele: TelethonService = context.bot_data["telethon"]
    registry: GroupRegistry = context.bot_data["registry"]
    await tele.refresh_dialogs_into_registry()
    n = await registry.run_bot_membership_probe(context.bot, context.bot.id)
    await update.effective_message.reply_text(
        f"Telethon diyalogları yenilendi; bot üyeliği doğrulanan ortak grup: {n}."
    )


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.my_chat_member:
        return
    chat = update.my_chat_member.chat
    new = update.my_chat_member.new_chat_member
    registry: GroupRegistry = context.bot_data["registry"]
    if new.user.id != context.bot.id:
        return
    if chat.type not in ("group", "supergroup", "channel"):
        return
    if _is_bot_still_member(new.status):
        await registry.note_bot_chat(chat.id, getattr(chat, "title", None) or None)
    else:
        await registry.remove_bot_chat(chat.id)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    registry: GroupRegistry = context.bot_data["registry"]
    await registry.note_bot_chat(chat.id, getattr(chat, "title", None) or None)


def build_application(
    token: str,
    registry: GroupRegistry,
    tele: TelethonService,
    admin_user_id: int | None = None,
) -> Application:
    app = (
        Application.builder()
        .token(token)
        .build()
    )
    app.bot_data["registry"] = registry
    app.bot_data["telethon"] = tele
    app.bot_data["admin_user_id"] = admin_user_id

    app.add_handler(build_invite_joined_callback())
    app.add_handler(build_invite_conversation())
    app.add_handler(build_bulk_kick_conversation())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("joint", cmd_joint))
    app.add_handler(CommandHandler("chat", cmd_chat))
    app.add_handler(CommandHandler("members", cmd_members))
    app.add_handler(CommandHandler("members_id", cmd_members_id))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_group_message))
    return app

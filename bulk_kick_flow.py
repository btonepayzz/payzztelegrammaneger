"""
Özel sohbette: kullanıcı adı → ortak gruplarda üyelik → tümü veya numara seçimi → önce Bot API, sonra Telethon.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from admin_gate import gate_operator_or_reply
from group_registry import GroupRegistry
from telethon_service import TelethonService

log = logging.getLogger(__name__)

BK_USERNAME, BK_MENU, BK_PICK, BK_CONFIRM = range(4)


def _private_only(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


def _strip_username(text: str) -> str:
    t = text.strip().split()[0] if text.strip() else ""
    if t.startswith("@"):
        t = t[1:]
    return t.strip()


def _target_is_member(status: Any) -> bool:
    return status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.OWNER,
    )


async def _groups_where_target_present(
    bot,
    tele: TelethonService,
    user_id: int,
    joint_ids: set[int],
    titles: dict[int, str],
) -> list[tuple[int, str]]:
    sem = asyncio.Semaphore(8)
    ok: list[tuple[int, str]] = []

    async def check(cid: int) -> None:
        async with sem:
            title = titles.get(cid) or str(cid)
            try:
                m = await bot.get_chat_member(cid, user_id)
                if _target_is_member(m.status):
                    ok.append((cid, title))
                    return
            except TelegramError:
                pass
            except Exception:
                log.debug("get_chat_member bot cid=%s", cid, exc_info=True)
            if await tele.is_user_in_chat(cid, user_id):
                ok.append((cid, title))

    await asyncio.gather(*(check(cid) for cid in joint_ids))
    uniq: dict[int, str] = {}
    for cid, title in ok:
        uniq[cid] = title
    return sorted(uniq.items(), key=lambda x: x[0])


async def _kick_one(
    bot,
    tele: TelethonService,
    chat_id: int,
    user_id: int,
) -> tuple[str, str]:
    try:
        await bot.ban_chat_member(chat_id, user_id)
        return ("Bot API", "")
    except TelegramError as e:
        ok, err = await tele.kick_user(chat_id, user_id)
        if ok:
            return ("Telethon", "")
        return ("Başarısız", f"Bot API: {e!s} | Telethon: {err}")
    except Exception as e:
        ok, err = await tele.kick_user(chat_id, user_id)
        if ok:
            return ("Telethon", "")
        return ("Başarısız", str(e) if not err else err)


async def _run_kicks(bot, tele: TelethonService, user_id: int, chat_ids: list[int]) -> list[str]:
    lines: list[str] = ["Sonuçlar:"]
    for cid in chat_ids:
        info = await tele.resolve_chat(cid)
        title = str(cid)
        if info.get("ok"):
            title = (info.get("title") or "").strip() or str(cid)
        how, err = await _kick_one(bot, tele, cid, user_id)
        if how == "Başarısız":
            lines.append(f"• {title}\n  {how}: {err}")
        else:
            lines.append(f"• {title}\n  Kullanılan: {how} ✓")
    return lines


def _chunk_lines(lines: list[str], max_len: int = 3900) -> list[str]:
    blocks: list[str] = []
    cur = ""
    for line in lines:
        if len(cur) + len(line) + 1 > max_len:
            if cur:
                blocks.append(cur.rstrip())
            cur = line + "\n"
        else:
            cur += line + "\n"
    if cur.strip():
        blocks.append(cur.rstrip())
    return blocks if blocks else ["(boş)"]


async def api_bulk_kick_preview(
    bot,
    tele: TelethonService,
    registry: GroupRegistry,
    username_raw: str,
) -> dict[str, Any]:
    """Web panel: hedef kullanıcının üye olduğu ortak grupları listeler."""
    uname = _strip_username(username_raw)
    if not re.match(r"^[A-Za-z0-9_]{5,32}$", uname):
        return {"ok": False, "error": "Geçersiz kullanıcı adı"}
    try:
        ent = await tele.client.get_entity(uname)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    from telethon.tl.types import User as TlUser

    if not isinstance(ent, TlUser):
        return {"ok": False, "error": "Bu bir kullanıcı hesabı değil."}
    uid = int(ent.id)
    me = await bot.get_me()
    await registry.run_bot_membership_probe(bot, me.id)
    snap = await registry.snapshot()
    if not snap.chat_ids:
        return {"ok": False, "error": "Ortak grup yok; önce /refresh."}
    present = await _groups_where_target_present(bot, tele, uid, snap.chat_ids, snap.titles)
    if not present:
        return {
            "ok": True,
            "username": uname,
            "target_id": uid,
            "groups": [],
            "count": 0,
            "message": "Hedef hiçbir ortak grupta üye görünmüyor.",
        }
    groups = [{"chat_id": cid, "title": title} for cid, title in present]
    return {
        "ok": True,
        "username": uname,
        "target_id": uid,
        "groups": groups,
        "count": len(groups),
    }


async def api_bulk_kick_all_groups(
    bot,
    tele: TelethonService,
    registry: GroupRegistry,
    username_raw: str,
    *,
    chat_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Web panel: ortak gruplarda hedefi çıkar. chat_ids verilirse yalnızca bu gruplarda."""
    uname = _strip_username(username_raw)
    if not re.match(r"^[A-Za-z0-9_]{5,32}$", uname):
        return {"ok": False, "error": "Geçersiz kullanıcı adı"}
    try:
        ent = await tele.client.get_entity(uname)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    from telethon.tl.types import User as TlUser

    if not isinstance(ent, TlUser):
        return {"ok": False, "error": "Bu bir kullanıcı hesabı değil."}
    uid = int(ent.id)
    me = await bot.get_me()
    await registry.run_bot_membership_probe(bot, me.id)
    snap = await registry.snapshot()
    if not snap.chat_ids:
        return {"ok": False, "error": "Ortak grup yok; önce /refresh."}
    present = await _groups_where_target_present(bot, tele, uid, snap.chat_ids, snap.titles)
    if not present:
        return {"ok": False, "error": "Hedef ortak gruplarda üye görünmüyor."}

    present_ids = {cid for cid, _ in present}
    pick: list[tuple[int, str]]
    ignored: list[int] = []

    if chat_ids is not None:
        want = set(chat_ids)
        pick = [(cid, t) for cid, t in present if cid in want]
        ignored = sorted(want - present_ids)
        if not pick:
            return {
                "ok": False,
                "error": "Seçilen gruplarda hedef üye yok veya seçim geçersiz.",
            }
    else:
        pick = list(present)

    out_lines = await _run_kicks(bot, tele, uid, [p[0] for p in pick])
    result: dict[str, Any] = {
        "ok": True,
        "username": uname,
        "target_id": uid,
        "group_count": len(pick),
        "lines": out_lines,
        "subset": chat_ids is not None,
    }
    if ignored:
        result["ignored_chat_ids"] = ignored
    return result


async def bulk_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message:
        return ConversationHandler.END
    if not _private_only(update):
        await update.effective_message.reply_text("Bu komut yalnızca bota özel mesajda kullanılabilir.")
        return ConversationHandler.END
    if not await gate_operator_or_reply(update, context.bot_data):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "Çıkarılacak kullanıcının kullanıcı adını yaz (@ ile veya @ olmadan).\n"
        "İptal için /iptal veya /cancel."
    )
    return BK_USERNAME


async def bulk_receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message or not update.message:
        return BK_USERNAME
    raw = update.message.text or ""
    uname = _strip_username(raw)
    if not re.match(r"^[A-Za-z0-9_]{5,32}$", uname):
        await update.message.reply_text("Geçerli bir Telegram kullanıcı adı gir (5–32 karakter, harf/rakam/_).")
        return BK_USERNAME

    tele: TelethonService = context.bot_data["telethon"]
    registry: GroupRegistry = context.bot_data["registry"]

    try:
        ent = await tele.client.get_entity(uname)
    except Exception as e:
        await update.message.reply_text(f"Kullanıcı bulunamadı veya çözümlenemedi: {e}")
        return BK_USERNAME

    from telethon.tl.types import User as TlUser

    if not isinstance(ent, TlUser):
        await update.message.reply_text("Bu bir kullanıcı hesabı değil.")
        return BK_USERNAME

    uid = int(ent.id)
    label = f"@{uname}"

    await update.message.reply_text("Ortak gruplar taranıyor, birkaç saniye sürebilir…")
    await registry.run_bot_membership_probe(context.bot, context.bot.id)
    snap = await registry.snapshot()
    if not snap.chat_ids:
        await update.message.reply_text(
            "Önce ortak grup oluştur: /refresh sonra tekrar dene."
        )
        return ConversationHandler.END

    present = await _groups_where_target_present(
        context.bot,
        tele,
        uid,
        snap.chat_ids,
        snap.titles,
    )
    if not present:
        await update.message.reply_text(
            f"{label} hiçbir ortak grupta üye görünmüyor (veya bot üye listesini göremiyor)."
        )
        return ConversationHandler.END

    context.user_data["kick_uid"] = uid
    context.user_data["kick_label"] = label
    context.user_data["kick_groups"] = present

    lines = [f"{label} şu ortak gruplarda üye:\n"]
    for i, (cid, title) in enumerate(present, start=1):
        lines.append(f"{i}. {title}\n   id: {cid}")
    body = "\n".join(lines)
    if len(body) > 3500:
        body = body[:3490] + "\n… (liste kısaltıldı)"

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Tüm bu gruplardan çıkar", callback_data="bulk:all")],
            [InlineKeyboardButton("Sadece seçtiklerimden çıkar", callback_data="bulk:pick")],
            [InlineKeyboardButton("İptal", callback_data="bulk:cancel")],
        ]
    )
    await update.message.reply_text(body, reply_markup=kb)
    return BK_MENU


async def bulk_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return BK_MENU
    await q.answer()
    data = (q.data or "")[:32]

    if data == "bulk:cancel":
        context.user_data.clear()
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("İptal edildi.")
        return ConversationHandler.END

    if data == "bulk:pick":
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(
            "Çıkarmak istediğin grupların numaralarını virgülle yaz.\n"
            "Örnek: 1, 3, 4\nİptal: /iptal"
        )
        return BK_PICK

    if data == "bulk:all":
        n = len(context.user_data.get("kick_groups") or [])
        await q.edit_message_reply_markup(reply_markup=None)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Evet, tümünden çıkar", callback_data="bulk:yes"),
                    InlineKeyboardButton("Hayır", callback_data="bulk:no"),
                ]
            ]
        )
        await q.message.reply_text(
            f"Toplam {n} gruptan kullanıcı çıkarılacak. Onaylıyor musun?",
            reply_markup=kb,
        )
        return BK_CONFIRM

    return BK_MENU


async def bulk_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not q:
        return BK_CONFIRM
    await q.answer()
    data = q.data or ""

    if data == "bulk:no":
        context.user_data.clear()
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("İptal edildi.")
        return ConversationHandler.END

    if data != "bulk:yes":
        return BK_CONFIRM

    await q.edit_message_reply_markup(reply_markup=None)
    groups: list[tuple[int, str]] = context.user_data.get("kick_groups") or []
    uid = int(context.user_data["kick_uid"])
    tele: TelethonService = context.bot_data["telethon"]

    await q.message.reply_text("İşlem yapılıyor…")
    out_lines = await _run_kicks(context.bot, tele, uid, [g[0] for g in groups])
    context.user_data.clear()
    for block in _chunk_lines(out_lines):
        await q.message.reply_text(block)
    return ConversationHandler.END


async def bulk_pick_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return BK_PICK
    text = update.message.text.strip()
    groups: list[tuple[int, str]] = context.user_data.get("kick_groups") or []
    if not groups:
        await update.message.reply_text("Oturum süresi doldu. /toplucikar ile yeniden başla.")
        return ConversationHandler.END

    nums: list[int] = []
    for part in re.split(r"[,;\s]+", text):
        part = part.strip()
        if not part:
            continue
        try:
            nums.append(int(part))
        except ValueError:
            await update.message.reply_text("Sadece numaralar kullan (örn: 1, 2, 4).")
            return BK_PICK

    picked: list[tuple[int, str]] = []
    for n in sorted(set(nums)):
        if 1 <= n <= len(groups):
            picked.append(groups[n - 1])
        else:
            await update.message.reply_text(f"Geçersiz numara: {n} (1–{len(groups)}).")
            return BK_PICK

    if not picked:
        await update.message.reply_text("En az bir numara seç.")
        return BK_PICK

    uid = int(context.user_data["kick_uid"])
    tele: TelethonService = context.bot_data["telethon"]
    await update.message.reply_text(f"{len(picked)} grupta işlem yapılıyor…")
    out_lines = await _run_kicks(context.bot, tele, uid, [g[0] for g in picked])
    context.user_data.clear()
    for block in _chunk_lines(out_lines):
        await update.message.reply_text(block)
    return ConversationHandler.END


async def bulk_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await gate_operator_or_reply(update, context.bot_data):
        return ConversationHandler.END
    context.user_data.clear()
    if update.effective_message:
        await update.effective_message.reply_text("İptal edildi.")
    return ConversationHandler.END


def build_bulk_kick_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("toplucikar", bulk_entry)],
        states={
            BK_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bulk_receive_username),
            ],
            BK_MENU: [
                CallbackQueryHandler(bulk_menu_cb, pattern=r"^bulk:(all|pick|cancel)$"),
            ],
            BK_PICK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bulk_pick_numbers),
            ],
            BK_CONFIRM: [
                CallbackQueryHandler(bulk_confirm_cb, pattern=r"^bulk:(yes|no)$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", bulk_cancel),
            CommandHandler("iptal", bulk_cancel),
        ],
        name="bulk_kick",
        persistent=False,
    )

"""
Davet linki akışı: ortak gruplar için önce Bot API, olmazsa Telethon ile link üretir;
Telethon hedef kullanıcıya DM atar; özelde /davet ile butonlu linkler + Katıldım doğrulaması.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from admin_gate import gate_operator_or_reply
from group_registry import GroupRegistry
from invite_store import append_bot_keyboard_messages, record_invite_package
from telethon_service import TelethonService

log = logging.getLogger(__name__)

INV_ASK_USERNAME = 0
INVITE_PENDING_TTL_SEC = 7 * 24 * 3600
INVITE_TRACKING_TTL_SEC = 7 * 24 * 3600
INVITE_GAP_SEC = 0.45
MAX_URL_BUTTONS_PER_MSG = 98


def _member_ok(status: Any) -> bool:
    return status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.OWNER,
    )


async def _user_is_in_group(bot, tele: TelethonService, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        if _member_ok(m.status):
            return True
    except TelegramError:
        pass
    except Exception:
        log.debug("get_chat_member invite-check cid=%s", chat_id, exc_info=True)
    return await tele.is_user_in_chat(chat_id, user_id)


def _button_title(title: str, index: int) -> str:
    raw = (title or "Grup").strip()
    label = f"{index}. {raw}"
    if len(label.encode("utf-8")) > 60:
        label = label.encode("utf-8")[:57].decode("utf-8", "ignore") + "…"
    return label[:64]


@dataclass
class PendingInvite:
    entries: list[tuple[int, str, str]]
    operator_chat_id: int
    expires_at: float


@dataclass
class InviteTracking:
    entries: list[tuple[int, str, str]]
    expires_at: float


INVITE_PENDING: dict[int, PendingInvite] = {}
INVITE_TRACKING: dict[int, InviteTracking] = {}


def _strip_username(text: str) -> str:
    t = text.strip().split()[0] if text.strip() else ""
    if t.startswith("@"):
        t = t[1:]
    return t.strip()


def _humanize_invite_link_error(raw: str) -> str:
    """Bot/Telethon davet linki hatalarını panele kısa Türkçe özet döndürür (tam metin loglanır)."""
    s = (raw or "").strip().lower()
    if not s:
        return "Davet linki alınamadı."

    if any(
        k in s
        for k in (
            "not enough rights",
            "invite link",
            "chat admin",
            "admin privileges",
            "exportchatinvite",
            "chat_admin_required",
            "invite_users",
            "user_admin_invalid",
            "need administrator rights",
            "required to do that in the specified chat",
        )
    ):
        return (
            "Davet linki için yetki yetersiz: hesap bu grupta yönetici olmalı ve "
            "«üyeler davet bağlantısı ile eklenebilir» iznine sahip olmalı."
        )

    if "flood" in s or "too many requests" in s:
        return "Çok sık istek; Telegram geçici limit koydu. Bir süre sonra yeniden deneyin."

    if ("peer" in s or "chat_id" in s) and "invalid" in s:
        return "Grup bulunamadı veya geçersiz."

    if "forbidden" in s:
        return "Bu gruba erişim engellenmiş veya işlem yasak."

    return "Davet linki alınamadı; grup türünü ve bot ile bağlı hesabın yönetici izinlerini kontrol edin."


async def _create_invite_joint(bot, tele: TelethonService, chat_id: int) -> tuple[bool, str, str]:
    """(ok, url_or_err_detail, method_label)"""
    try:
        inv = await bot.create_chat_invite_link(chat_id)
        return True, inv.invite_link, "Bot API"
    except Exception as e:
        ok, link = await tele.export_invite_link(chat_id)
        if ok:
            return True, link, "Telethon"
        bot_err = e if isinstance(e, TelegramError) else str(e)
        combined = f"{bot_err!s} | Telethon: {link}"
        log.warning("Davet linki üretilemedi chat_id=%s: %s", chat_id, combined)
        return False, _humanize_invite_link_error(combined), ""


async def _build_invites_for_joint_groups(
    bot,
    tele: TelethonService,
    registry: GroupRegistry,
) -> tuple[list[tuple[int, str, str, str]], list[str]]:
    """Başarılı (chat_id, title, url, method) ve hata satırları."""
    me_bot = await bot.get_me()
    await registry.run_bot_membership_probe(bot, me_bot.id)
    snap = await registry.snapshot()
    if not snap.chat_ids:
        return [], ["Ortak grup yok; önce /refresh dene."]

    ok_rows: list[tuple[int, str, str, str]] = []
    errors: list[str] = []
    for cid in sorted(snap.chat_ids):
        title = snap.titles.get(cid) or str(cid)
        await asyncio.sleep(INVITE_GAP_SEC)
        success, url_or_err, method = await _create_invite_joint(bot, tele, cid)
        if success:
            ok_rows.append((cid, title, url_or_err, method))
        else:
            errors.append(f"{title} ({cid}): {url_or_err}")
    return ok_rows, errors


async def _send_invite_keyboard_chunks(
    bot,
    chat_user_id: int,
    entries: list[tuple[int, str, str]],
    *,
    header: str,
) -> list[int]:
    """URL düğmeleri + son blokta Katıldım. Gönderilen her mesajın id listesi (panelden silme için)."""
    sent_ids: list[int] = []
    if not entries:
        return sent_ids
    n = len(entries)
    stride = MAX_URL_BUTTONS_PER_MSG
    parts = (n + stride - 1) // stride
    offset = 0
    part_num = 0
    while offset < n:
        part_num += 1
        chunk = entries[offset : offset + stride]
        offset += stride
        keyboard: list[list[InlineKeyboardButton]] = []
        base_idx = (part_num - 1) * stride
        for i, (_cid, title, url) in enumerate(chunk):
            keyboard.append(
                [InlineKeyboardButton(_button_title(title, base_idx + i + 1), url=url)]
            )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "✅ Katıldım — eksik varsa tekrar göster",
                    callback_data="inv:joined",
                )
            ]
        )
        cap = f"{header}\n(Bölüm {part_num}/{parts})" if parts > 1 else header
        msg = await bot.send_message(
            chat_id=chat_user_id,
            text=cap,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        sent_ids.append(msg.message_id)
    return sent_ids


async def invite_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message or update.effective_chat.type != "private":
        await update.effective_message.reply_text("Bu komut yalnızca bota özel mesajda kullanılabilir.")
        return ConversationHandler.END
    uid = update.effective_user.id if update.effective_user else None
    if uid is not None and uid in INVITE_PENDING:
        await deliver_invite_claim(update, context)
        return ConversationHandler.END
    if not await gate_operator_or_reply(update, context.bot_data):
        return ConversationHandler.END
    me = await context.bot.get_me()
    context.bot_data["bot_username"] = me.username or ""
    await update.effective_message.reply_text(
        "Davet edilecek kişinin kullanıcı adını yaz (@ olmadan veya @ ile).\n"
        "Linkler oluşturulacak, hesabım (Telethon) o kişiye talimat gönderecek.\n"
        "İptal: /iptal"
    )
    return INV_ASK_USERNAME


async def create_invite_package(
    bot,
    tele: TelethonService,
    registry: GroupRegistry,
    username_raw: str,
    *,
    operator_chat_id: int = 0,
) -> dict[str, Any]:
    """Telegram bot veya web panel için ortak davet oluşturma."""
    uname = _strip_username(username_raw)
    if not re.match(r"^[A-Za-z0-9_]{5,32}$", uname):
        return {"ok": False, "error": "Geçersiz kullanıcı adı (5–32 karakter)."}

    try:
        ent = await tele.client.get_entity(uname)
    except Exception as e:
        return {"ok": False, "error": f"Kullanıcı çözümlenemedi: {e}"}

    from telethon.tl.types import User as TlUser

    if not isinstance(ent, TlUser):
        return {"ok": False, "error": "Bu bir kullanıcı hesabı değil."}

    target_uid = int(ent.id)
    me = await bot.get_me()
    bot_uname = me.username or ""
    if not bot_uname:
        return {"ok": False, "error": "Bot kullanıcı adı yok (@BotFather)."}

    rows, errs = await _build_invites_for_joint_groups(bot, tele, registry)
    if not rows:
        return {
            "ok": False,
            "error": "Davet linki oluşturulamadı.",
            "details": errs,
        }

    entries = [(cid, t, u) for cid, t, u, _ in rows]
    INVITE_PENDING[target_uid] = PendingInvite(
        entries=entries,
        operator_chat_id=operator_chat_id,
        expires_at=time.time() + INVITE_PENDING_TTL_SEC,
    )

    dm = (
        "Merhaba,\n\n"
        "Katılman için gruplara özel davet linkleri hazırlandı.\n"
        "Bu linkler yalnızca senin hesabına iletilecek.\n\n"
        f"@{bot_uname} botuna özel mesajda bir kez şu komutu gönder:\n"
        "/davet\n\n"
        "Butonlarla gruplara katıl; «Katıldım» ile kontrol edebilirsin."
    )
    dm_ok = False
    dm_err: str | None = None
    telethon_dm_message_id: int | None = None
    try:
        sent = await tele.client.send_message(target_uid, dm)
        dm_ok = True
        telethon_dm_message_id = int(getattr(sent, "id", 0)) or None
    except Exception as e:
        log.exception("Telethon DM gönderilemedi")
        dm_err = str(e)

    record_invite_package(
        uname,
        target_uid,
        operator_chat_id,
        telethon_dm_message_id=telethon_dm_message_id,
    )

    return {
        "ok": True,
        "username": uname,
        "target_id": target_uid,
        "link_count": len(rows),
        "errors": errs,
        "dm_sent": dm_ok,
        "dm_error": dm_err,
        "sample": [{"title": t, "method": m} for _c, t, _u, m in rows[:5]],
    }


async def invite_receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await gate_operator_or_reply(update, context.bot_data):
        return ConversationHandler.END
    if not update.message or not update.message.text:
        return INV_ASK_USERNAME
    raw = update.message.text

    tele: TelethonService = context.bot_data["telethon"]
    registry: GroupRegistry = context.bot_data["registry"]

    await update.message.reply_text(
        "Ortak gruplar için davet linkleri oluşturuluyor (rate limit için yavaş gidebilir)…"
    )

    result = await create_invite_package(
        context.bot,
        tele,
        registry,
        raw,
        operator_chat_id=update.effective_chat.id,
    )
    if not result.get("ok"):
        msg = result.get("error", "Hata")
        det = result.get("details")
        if det:
            msg += "\n" + "\n".join(det[:20])
        await update.message.reply_text(msg[:4000])
        return ConversationHandler.END

    summary = [
        f"Hedef: @{result['username']} (id {result['target_id']})",
        f"Başarılı link: {result['link_count']}",
        "Örnek (ilk 3):",
    ]
    for s in result.get("sample", [])[:3]:
        summary.append(f"• {s['title']} — {s['method']}")
    if result.get("errors"):
        summary.append(f"\nUyarı: {len(result['errors'])} grupta link alınamadı.")
    if result.get("dm_sent"):
        summary.append("\nTelefon hesabından hedefe talimat DM olarak gönderildi.")
    elif result.get("dm_error"):
        summary.append(
            f"\nDM gönderilemedi: {result['dm_error']}\n"
            "Hedef yine de bota yazarsa linkler hazır."
        )
    await update.message.reply_text("\n".join(summary)[:4000])

    return ConversationHandler.END


async def invite_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await gate_operator_or_reply(update, context.bot_data):
        return ConversationHandler.END
    if update.effective_message:
        await update.effective_message.reply_text("İptal.")
    return ConversationHandler.END


async def deliver_invite_claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != "private":
        return
    uid = update.effective_user.id
    pending = INVITE_PENDING.get(uid)
    if not pending:
        return

    now = time.time()
    if now > pending.expires_at:
        INVITE_PENDING.pop(uid, None)
        if update.effective_message:
            await update.effective_message.reply_text(
                "Bu davet paketinin süresi doldu. Yöneticiden /davet ile yeniden istemen gerekir."
            )
        return

    entries = list(pending.entries)
    INVITE_PENDING.pop(uid, None)

    INVITE_TRACKING[uid] = InviteTracking(
        entries=entries,
        expires_at=time.time() + INVITE_TRACKING_TTL_SEC,
    )

    mids = await _send_invite_keyboard_chunks(
        context.bot,
        uid,
        entries,
        header="👇 Gruba katılmak için düğmelere dokun.\n"
        "Hepsine katıldıysan «Katıldım»a bas; eksik grupların linkleri tekrar gösterilir.",
    )
    append_bot_keyboard_messages(uid, mids)


async def invite_joined_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.from_user:
        return
    uid = q.from_user.id
    tele: TelethonService = context.bot_data["telethon"]

    track = INVITE_TRACKING.get(uid)
    if not track:
        await q.answer("Bu liste süresi doldu veya zaten tamamlandı.", show_alert=True)
        return

    if time.time() > track.expires_at:
        INVITE_TRACKING.pop(uid, None)
        await q.answer("Süre doldu.", show_alert=True)
        return

    entries = track.entries
    if not entries:
        INVITE_TRACKING.pop(uid, None)
        await q.answer("Tamam.", show_alert=False)
        return

    sem = asyncio.Semaphore(10)

    async def check_one(item: tuple[int, str, str]) -> tuple[int, str, str] | None:
        cid, title, url = item
        async with sem:
            inside = await _user_is_in_group(context.bot, tele, cid, uid)
        return None if inside else item

    packed = await asyncio.gather(*(check_one(e) for e in entries))
    missing = [x for x in packed if x is not None]

    if not missing:
        INVITE_TRACKING.pop(uid, None)
        await q.answer("Hepsi tamam.")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        await context.bot.send_message(
            chat_id=uid,
            text="✅ Tüm listelenen gruplara üyesin. Hoş geldin!",
        )
        return

    await q.answer(f"{len(missing)} grup eksik — linkler yenilendi.")
    track.entries = missing
    INVITE_TRACKING[uid] = track

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    await context.bot.send_message(
        chat_id=uid,
        text=f"Hâlâ {len(missing)} gruba katılmadığın görünüyor. Aşağıdan tekrar dene:",
    )
    remids = await _send_invite_keyboard_chunks(
        context.bot,
        uid,
        missing,
        header="🔁 Henüz katılmadığın gruplar:",
    )
    append_bot_keyboard_messages(uid, remids)


async def revoke_invite_package(bot, tele: TelethonService, target_id: int) -> dict[str, Any]:
    """Panelden davet iptali: kaydı sil, bot klavye mesajlarını sil, Telethon DM ve diyalog temizliği."""
    from invite_store import pop_record

    tid = int(target_id)
    record = pop_record(tid)
    INVITE_PENDING.pop(tid, None)
    INVITE_TRACKING.pop(tid, None)

    warnings: list[str] = []
    if not record:
        return {
            "ok": True,
            "removed": False,
            "target_id": tid,
            "note": "Dosyada kayıt yoktu; bekleyen davet bellek temizlendi.",
            "warnings": warnings,
        }

    for mid in record.get("bot_keyboard_message_ids") or []:
        try:
            await bot.delete_message(chat_id=tid, message_id=int(mid))
        except TelegramError as e:
            warnings.append(f"Bot mesajı {mid}: {e}")
        except Exception as e:
            warnings.append(f"Bot mesajı {mid}: {e}")

    dm_id = record.get("telethon_dm_message_id")
    if dm_id is not None:
        try:
            await tele.ensure_connected()
            await tele.client.delete_messages(tid, int(dm_id))
        except Exception as e:
            warnings.append(f"Telethon talimat DM: {e}")

    try:
        await tele.ensure_connected()
        ent = await tele.client.get_entity(tid)
        await tele.client.delete_dialog(ent)
    except Exception as e:
        warnings.append(f"Telethon diyalog: {e}")

    return {
        "ok": True,
        "removed": True,
        "username": record.get("username"),
        "target_id": tid,
        "warnings": warnings,
    }


def build_invite_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("davet", invite_entry)],
        states={
            INV_ASK_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, invite_receive_username),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", invite_cancel),
            CommandHandler("iptal", invite_cancel),
        ],
        name="invite_flow",
        persistent=False,
    )


def build_invite_joined_callback() -> CallbackQueryHandler:
    return CallbackQueryHandler(invite_joined_callback, pattern=r"^inv:joined$")

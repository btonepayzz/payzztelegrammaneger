"""
Telethon kullanıcı hesabı: botun göremediği tam bilgi ve üye listeleri.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodWaitError, PhoneCodeInvalidError, SessionPasswordNeededError
from telethon.tl.types import Channel, Chat, User

from group_registry import GroupRegistry

log = logging.getLogger(__name__)


def _title_from_entity(entity: Any) -> str:
    if hasattr(entity, "title") and entity.title:
        return str(entity.title)
    if isinstance(entity, User):
        n = getattr(entity, "first_name", "") or ""
        ln = getattr(entity, "last_name", "") or ""
        return (n + " " + ln).strip() or str(entity.id)
    return ""


class TelethonService:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_name: str,
        registry: GroupRegistry,
    ) -> None:
        self._client = TelegramClient(session_name, api_id, api_hash)
        self._registry = registry
        self._refresh_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._pending_phone: str | None = None
        self._pending_phone_code_hash: str | None = None

    @property
    def client(self) -> TelegramClient:
        return self._client

    @property
    def login_code_pending(self) -> bool:
        return bool(self._pending_phone and self._pending_phone_code_hash)

    async def ensure_connected(self) -> None:
        if not self._client.is_connected():
            await self._client.connect()

    async def is_user_authorized(self) -> bool:
        await self.ensure_connected()
        return await self._client.is_user_authorized()

    async def connect_for_startup(self) -> None:
        """Bağlanır; oturum yoksa etkileşimli start() çağırmaz — panelden tamamlanır."""
        await self.ensure_connected()
        if await self._client.is_user_authorized():
            log.info("Telethon oturumu hazır")
        else:
            log.warning(
                "Telethon kullanıcı oturumu yok — panel Yönetim sayfasından Telegram ile giriş yapın."
            )

    def _clear_login_pending(self) -> None:
        self._pending_phone = None
        self._pending_phone_code_hash = None

    async def login_send_code(self, phone: str) -> dict[str, Any]:
        """SMS doğrulama kodu talep eder (panel akışı)."""
        phone = (phone or "").strip()
        if not phone:
            return {"ok": False, "error": "Telefon numarası gerekli"}
        async with self._login_lock:
            await self.ensure_connected()
            if await self._client.is_user_authorized():
                return {"ok": False, "error": "Zaten oturum açık"}
            try:
                sent = await self._client.send_code_request(phone)
            except FloodWaitError as e:
                sec = int(getattr(e, "seconds", 0) or 0)
                return {"ok": False, "error": f"Çok fazla deneme; yaklaşık {sec} sn sonra tekrar deneyin"}
            except Exception as e:
                return {"ok": False, "error": str(e)}
            self._pending_phone = phone
            self._pending_phone_code_hash = sent.phone_code_hash
            return {"ok": True}

    async def login_submit_code(self, code: str) -> dict[str, Any]:
        async with self._login_lock:
            code = (code or "").strip()
            if not code:
                return {"ok": False, "error": "Kod gerekli"}
            if not self._pending_phone or not self._pending_phone_code_hash:
                return {"ok": False, "error": "Önce telefon numarasına doğrulama kodu isteyin"}
            await self.ensure_connected()
            try:
                await self._client.sign_in(
                    self._pending_phone,
                    code,
                    phone_code_hash=self._pending_phone_code_hash,
                )
            except SessionPasswordNeededError:
                return {"ok": True, "need_password": True}
            except PhoneCodeInvalidError:
                self._clear_login_pending()
                return {"ok": False, "error": "Kod geçersiz veya süresi doldu; kodu yeniden isteyin"}
            except Exception as e:
                self._clear_login_pending()
                return {"ok": False, "error": str(e)}
            self._clear_login_pending()
            try:
                await self.refresh_dialogs_into_registry()
            except Exception:
                log.exception("Oturum sonrası dialog yenileme")
            return {"ok": True, "need_password": False}

    async def login_submit_password(self, password: str) -> dict[str, Any]:
        async with self._login_lock:
            password = (password or "").strip()
            if not password:
                return {"ok": False, "error": "İki aşamalı doğrulama şifresi gerekli"}
            await self.ensure_connected()
            try:
                await self._client.sign_in(password=password)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            self._clear_login_pending()
            try:
                await self.refresh_dialogs_into_registry()
            except Exception:
                log.exception("Oturum sonrası dialog yenileme")
            return {"ok": True}

    async def refresh_dialogs_into_registry(self) -> None:
        """Telethon'un gördüğü grup/kanal sohbetlerini registry'ye yazar (dialog.id = Bot API chat_id ile uyumlu)."""
        await self.ensure_connected()
        if not await self._client.is_user_authorized():
            log.debug("Telethon oturumu yok; dialog listesi yenilenmedi")
            return
        async with self._refresh_lock:
            ids: set[int] = set()
            titles: dict[int, str] = {}
            async for dialog in self._client.iter_dialogs():
                ent = dialog.entity
                if isinstance(ent, Chat):
                    pass
                elif isinstance(ent, Channel):
                    if getattr(ent, "broadcast", False) and not getattr(ent, "megagroup", False):
                        continue
                else:
                    continue
                cid = int(dialog.id)
                ids.add(cid)
                name = dialog.name or _title_from_entity(dialog.entity)
                if name:
                    titles[cid] = name
            await self._registry.set_telethon_chats(ids, titles)

    async def resolve_chat(self, chat_id: int) -> dict[str, Any]:
        """Bot API'nin eksik bıraktığı başlık / kullanıcı adı / üye sayısı."""
        try:
            ent = await self._client.get_entity(chat_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        title = getattr(ent, "title", None) or ""
        username = getattr(ent, "username", None)
        participants_count = getattr(ent, "participants_count", None)
        return {
            "ok": True,
            "id": chat_id,
            "title": title,
            "username": username,
            "participants_count": participants_count,
        }

    async def list_participants(
        self,
        chat_id: int,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Grup üyeleri (genelde admin gerektirir; büyük gruplarda limit kullanın)."""
        try:
            participants = await self._client.get_participants(chat_id, limit=limit)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        rows: list[dict[str, Any]] = []
        for u in participants:
            if not isinstance(u, User):
                continue
            name = (u.first_name or "") + (" " + (u.last_name or "") if u.last_name else "")
            rows.append(
                {
                    "id": u.id,
                    "username": u.username,
                    "name": name.strip() or str(u.id),
                    "bot": bool(u.bot),
                }
            )
        return {"ok": True, "chat_id": chat_id, "count": len(rows), "members": rows}

    async def is_user_in_chat(self, chat_id: int, user_id: int) -> bool:
        """Hedef kullanıcının bu sohbette üye olup olmadığı (get_chat_member başarısızsa yedek)."""
        try:
            chat_ent = await self._client.get_entity(chat_id)
            user_ent = await self._client.get_entity(user_id)
            await self._client.get_permissions(chat_ent, user_ent)
            return True
        except Exception:
            return False

    async def kick_user(self, chat_id: int, user_id: int) -> tuple[bool, str]:
        """Üyeyi gruptan çıkarır (Telethon hesabı gerekli yetkilere sahip olmalı)."""
        try:
            chat_ent = await self._client.get_entity(chat_id)
            await self._client.kick_participant(chat_ent, user_id)
            return True, ""
        except Exception as e:
            return False, str(e)

    async def export_invite_link(self, chat_id: int) -> tuple[bool, str]:
        """Grup davet linki (Telethon hesabının davet oluşturma yetkisi olmalı)."""
        try:
            from telethon.tl.functions.messages import ExportChatInviteRequest

            ent = await self._client.get_entity(chat_id)
            inp = await self._client.get_input_entity(ent)
            result = await self._client(ExportChatInviteRequest(peer=inp))
            return True, result.link
        except Exception as e:
            return False, str(e)

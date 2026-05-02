"""
Telethon kullanıcı hesabı: botun göremediği tam bilgi ve üye listeleri.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import TelegramClient
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

    @property
    def client(self) -> TelegramClient:
        return self._client

    async def connect_and_login(self) -> None:
        await self._client.connect()
        if not await self._client.is_user_authorized():
            await self._client.start()
        log.info("Telethon oturumu hazır")

    async def refresh_dialogs_into_registry(self) -> None:
        """Telethon'un gördüğü grup/kanal sohbetlerini registry'ye yazar (dialog.id = Bot API chat_id ile uyumlu)."""
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

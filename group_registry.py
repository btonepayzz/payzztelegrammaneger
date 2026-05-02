"""
Telethon diyalogları + Bot API ile üyelik doğrulaması.
Bot API'nin 'tüm gruplarım' listesi yok; güncelleme dışı gruplar için get_chat_member taraması gerekir.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from telegram import Bot
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

log = logging.getLogger(__name__)


@dataclass
class JointGroupSnapshot:
    """İki tarafta da bilinen ortak gruplar (chat_id -> özet)."""

    chat_ids: set[int] = field(default_factory=set)
    titles: dict[int, str] = field(default_factory=dict)
    updated_at: float | None = None


def _member_means_bot_in_group(status: Any) -> bool:
    return status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.OWNER,
    )


class GroupRegistry:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._bot_chats: set[int] = set()
        self._bot_probe_ok: set[int] = set()
        self._telethon_chats: set[int] = set()
        self._titles: dict[int, str] = {}
        self._joint: JointGroupSnapshot = JointGroupSnapshot()

    async def note_bot_chat(self, chat_id: int, title: str | None = None) -> None:
        async with self._lock:
            self._bot_chats.add(chat_id)
            if title:
                self._titles[chat_id] = title
            self._recompute_locked()

    async def remove_bot_chat(self, chat_id: int) -> None:
        async with self._lock:
            self._bot_chats.discard(chat_id)
            self._recompute_locked()

    async def set_telethon_chats(self, ids: set[int], titles: dict[int, str]) -> None:
        async with self._lock:
            self._telethon_chats = set(ids)
            for cid, t in titles.items():
                if t:
                    self._titles[cid] = t
            self._recompute_locked()

    def _recompute_locked(self) -> None:
        bot_anywhere = self._bot_chats | self._bot_probe_ok
        joint_ids = self._telethon_chats & bot_anywhere
        self._joint = JointGroupSnapshot(
            chat_ids=joint_ids,
            titles={cid: self._titles.get(cid, "") for cid in joint_ids},
        )

    async def run_bot_membership_probe(self, bot: Bot, bot_user_id: int) -> int:
        """Telethon'daki her grup için Bot API ile bot üyeliğini doğrular (sessiz gruplar dahil)."""
        async with self._lock:
            candidates = frozenset(self._telethon_chats)
            titles = dict(self._titles)

        if not candidates:
            async with self._lock:
                self._bot_probe_ok = set()
                self._recompute_locked()
            return 0

        sem = asyncio.Semaphore(6)
        verified: set[int] = set()

        async def check(cid: int) -> None:
            async with sem:
                try:
                    m = await bot.get_chat_member(cid, bot_user_id)
                    if _member_means_bot_in_group(m.status):
                        verified.add(cid)
                except TelegramError:
                    pass
                except Exception:
                    log.exception("get_chat_member beklenmeyen hata chat_id=%s", cid)

        await asyncio.gather(*(check(cid) for cid in candidates))

        async with self._lock:
            self._bot_probe_ok = verified
            self._recompute_locked()

        log.info(
            "Bot üyelik taraması: Telethon %s sohbet, bot doğrulanan %s ortak grup",
            len(candidates),
            len(verified),
        )
        return len(verified)

    async def snapshot(self) -> JointGroupSnapshot:
        async with self._lock:
            return JointGroupSnapshot(
                chat_ids=set(self._joint.chat_ids),
                titles=dict(self._joint.titles),
            )

    async def bot_known_ids(self) -> set[int]:
        async with self._lock:
            return set(self._bot_chats)

    async def telethon_known_ids(self) -> set[int]:
        async with self._lock:
            return set(self._telethon_chats)

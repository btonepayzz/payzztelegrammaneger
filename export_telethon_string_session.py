"""
Yerelde bir kez çalıştır: Telethon telefon doğrulamasından sonra TELETHON_STRING_SESSION üretir.
Çıktıyı Railway Variables'a TELETHON_STRING_SESSION olarak ekle (repoya koyma).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


async def main() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"].strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()
    saved = client.session.save()
    print("Railway → Variables → şunu ekle (tek satır, kimseyle paylaşma):")
    print(f"TELETHON_STRING_SESSION={saved}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

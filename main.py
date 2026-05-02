"""
Tek süreç: Telethon + Bot API; ortak gruplar registry'de birleşir; aiohttp iç API + Node web panel.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from telegram import Update

from config import load_settings
from group_registry import GroupRegistry
from internal_api import start_internal_api
from telegram_bot import build_application
from telethon_service import TelethonService

ROOT = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")


async def periodic_sync(
    tele: TelethonService,
    registry: GroupRegistry,
    bot,
    interval: int,
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await tele.refresh_dialogs_into_registry()
            me = await bot.get_me()
            await registry.run_bot_membership_probe(bot, me.id)
        except Exception:
            log.exception("Periyodik senkron hatası")


def _panel_env(settings, internal_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env["INTERNAL_API_URL"] = internal_url
    env["INTERNAL_PANEL_TOKEN"] = settings.internal_panel_token
    env["WEB_PANEL_PORT"] = str(settings.web_panel_port)
    env["WEB_PANEL_BIND_HOST"] = settings.web_panel_bind_host
    env["WEB_PANEL_USER"] = settings.web_panel_user
    env["WEB_PANEL_SESSION_SECRET"] = settings.web_panel_session_secret
    env["NODE_ENV"] = "production"
    if settings.web_panel_password_plain:
        env["WEB_PANEL_PASSWORD"] = settings.web_panel_password_plain
    if settings.web_panel_password_hash:
        env["WEB_PANEL_PASSWORD_HASH"] = settings.web_panel_password_hash
    if settings.web_panel_totp_secret:
        env["WEB_PANEL_TOTP_SECRET"] = settings.web_panel_totp_secret
    return env


def _should_start_panel(settings) -> bool:
    if not settings.web_panel_enabled or not settings.internal_panel_token:
        return False
    if not settings.web_panel_user.strip():
        return False
    if not settings.web_panel_password_plain and not settings.web_panel_password_hash:
        log.warning(
            "Web panel: WEB_PANEL_PASSWORD veya WEB_PANEL_PASSWORD_HASH tanımlı değil — panel başlatılmadı."
        )
        return False
    return True


async def main() -> None:
    settings = load_settings()
    registry = GroupRegistry()
    tele = TelethonService(
        settings.api_id,
        settings.api_hash,
        settings.telethon_session,
        registry,
    )
    await tele.connect_and_login()
    await tele.refresh_dialogs_into_registry()

    app = build_application(settings.bot_token, registry, tele, settings.admin_user_id)
    await app.initialize()
    me = await app.bot.get_me()
    await registry.run_bot_membership_probe(app.bot, me.id)
    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    internal_runner = None
    internal_url = f"http://{settings.internal_api_host}:{settings.internal_api_port}"
    if settings.internal_panel_token:
        internal_runner = await start_internal_api(
            registry,
            tele,
            app.bot,
            settings.internal_api_host,
            settings.internal_api_port,
            settings.internal_panel_token,
        )
    else:
        log.warning(
            "INTERNAL_PANEL_TOKEN .env içinde yok — iç API ve web panel başlatılmaz. "
            ".env.example satırlarını ekle."
        )

    node_proc: subprocess.Popen | None = None
    if internal_runner and _should_start_panel(settings):
        node_exe = shutil.which("node")
        wp = ROOT / "webpanel" / "server.js"
        if node_exe and wp.is_file():
            if not (ROOT / "webpanel" / "node_modules").is_dir():
                log.warning(
                    "webpanel/node_modules bulunamadı. Bir kez çalıştır: cd webpanel && npm install"
                )
            try:
                node_proc = subprocess.Popen(
                    [node_exe, str(wp)],
                    cwd=str(ROOT / "webpanel"),
                    env=_panel_env(settings, internal_url),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if settings.web_panel_bind_host in ("0.0.0.0", "::"):
                    log.info(
                        "Web panel: %s:%s (dış erişim — Railway/PaaS ortam değişkeni PORT ile uyumlu)",
                        settings.web_panel_bind_host,
                        settings.web_panel_port,
                    )
                else:
                    log.info(
                        "Web panel: http://127.0.0.1:%s (yalnızca localhost)",
                        settings.web_panel_port,
                    )
                await asyncio.sleep(0.5)
                if node_proc.poll() is not None:
                    log.error(
                        "Node panel hemen kapandı (exit %s). webpanel dizininde çalıştır: npm install",
                        node_proc.returncode,
                    )
            except Exception:
                log.exception("Web panel başlatılamadı")
        else:
            log.warning("node bulunamadı veya webpanel/server.js yok — panel atlandı.")
    elif internal_runner:
        log.warning(
            "Web panel başlatılmadı: WEB_PANEL_USER ve WEB_PANEL_PASSWORD (veya HASH) .env içinde olmalı."
        )

    refresh_task = asyncio.create_task(
        periodic_sync(tele, registry, app.bot, settings.sync_interval_sec),
        name="telethon-bot-sync",
    )

    log.info("Bot çalışıyor; Ctrl+C ile çık.")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        if node_proc is not None:
            node_proc.terminate()
            try:
                node_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                node_proc.kill()
            except Exception:
                log.exception("Panel süreci sonlandırılamadı")
        if internal_runner is not None:
            await internal_runner.cleanup()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await tele.client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Kapatılıyor.")

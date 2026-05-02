"""
Yerel iç HTTP API (aiohttp). Web panel (Node) Bearer token ile buraya bağlanır.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from bulk_kick_flow import api_bulk_kick_all_groups, api_bulk_kick_preview
from invite_flow import create_invite_package, revoke_invite_package
from invite_store import list_recipients

log = logging.getLogger(__name__)

_TELETHON_REQUIRED_MSG = (
    "Telethon kullanıcı oturumu yok — panel Yönetim → Telegram oturumu bölümünden giriş yapın."
)


async def _telethon_required(tele: Any) -> web.Response | None:
    if not await tele.is_user_authorized():
        return web.json_response({"ok": False, "error": _TELETHON_REQUIRED_MSG}, status=503)
    return None


@web.middleware
async def bearer_auth(request: web.Request, handler: Any) -> web.StreamResponse:
    if request.path == "/health":
        return await handler(request)
    auth = request.headers.get("Authorization", "")
    expected = "Bearer " + request.app["internal_token"]
    if auth != expected:
        return web.json_response({"error": "yetkisiz"}, status=401)
    return await handler(request)


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "telegram-bot-internal"})


async def handle_joint(request: web.Request) -> web.Response:
    tele = request.app["tele"]
    if not await tele.is_user_authorized():
        return web.json_response(
            {
                "groups": [],
                "count": 0,
                "warning": "Telethon oturumu yok — ortak grup listesi boş; Yönetim sayfasından Telegram ile giriş yapın.",
            }
        )
    registry = request.app["registry"]
    bot = request.app["bot"]
    me = await bot.get_me()
    await registry.run_bot_membership_probe(bot, me.id)
    snap = await registry.snapshot()
    groups = [{"chat_id": cid, "title": snap.titles.get(cid, "")} for cid in sorted(snap.chat_ids)]
    return web.json_response({"groups": groups, "count": len(groups)})


async def handle_refresh(request: web.Request) -> web.Response:
    tele = request.app["tele"]
    deny = await _telethon_required(tele)
    if deny is not None:
        return deny
    registry = request.app["registry"]
    bot = request.app["bot"]
    await tele.refresh_dialogs_into_registry()
    me = await bot.get_me()
    n = await registry.run_bot_membership_probe(bot, me.id)
    return web.json_response({"ok": True, "verified_joint_groups": n})


async def handle_invite(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Geçersiz JSON"}, status=400)
    username = (data.get("username") or "").strip()
    if not username:
        return web.json_response({"ok": False, "error": "username gerekli"}, status=400)
    bot = request.app["bot"]
    tele = request.app["tele"]
    deny = await _telethon_required(tele)
    if deny is not None:
        return deny
    registry = request.app["registry"]
    result = await create_invite_package(bot, tele, registry, username, operator_chat_id=0)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def handle_invite_recipients(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "recipients": list_recipients()})


async def handle_invite_revoke(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Geçersiz JSON"}, status=400)
    raw_id = data.get("target_id")
    try:
        target_id = int(raw_id)
    except (TypeError, ValueError):
        return web.json_response(
            {"ok": False, "error": "target_id sayısal olmalı"},
            status=400,
        )
    bot = request.app["bot"]
    tele = request.app["tele"]
    deny = await _telethon_required(tele)
    if deny is not None:
        return deny
    result = await revoke_invite_package(bot, tele, target_id)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def handle_kick_preview(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Geçersiz JSON"}, status=400)
    username = (data.get("username") or "").strip()
    if not username:
        return web.json_response({"ok": False, "error": "username gerekli"}, status=400)
    bot = request.app["bot"]
    tele = request.app["tele"]
    deny = await _telethon_required(tele)
    if deny is not None:
        return deny
    registry = request.app["registry"]
    result = await api_bulk_kick_preview(bot, tele, registry, username)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def handle_kick(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Geçersiz JSON"}, status=400)
    username = (data.get("username") or "").strip()
    if not username:
        return web.json_response({"ok": False, "error": "username gerekli"}, status=400)

    chat_ids: list[int] | None = None
    if "chat_ids" in data and data["chat_ids"] is not None:
        raw = data["chat_ids"]
        if not isinstance(raw, list):
            return web.json_response(
                {"ok": False, "error": "chat_ids bir sayı listesi olmalı"},
                status=400,
            )
        try:
            chat_ids = [int(x) for x in raw]
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error": "chat_ids yalnızca tam sayı içerebilir"},
                status=400,
            )
        if len(chat_ids) == 0:
            return web.json_response(
                {"ok": False, "error": "En az bir grup seçin veya tümü için chat_ids göndermeyin."},
                status=400,
            )

    bot = request.app["bot"]
    tele = request.app["tele"]
    deny = await _telethon_required(tele)
    if deny is not None:
        return deny
    registry = request.app["registry"]
    result = await api_bulk_kick_all_groups(bot, tele, registry, username, chat_ids=chat_ids)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def handle_telethon_status(request: web.Request) -> web.Response:
    tele = request.app["tele"]
    await tele.ensure_connected()
    authorized = await tele.is_user_authorized()
    return web.json_response(
        {
            "ok": True,
            "authorized": authorized,
            "login_code_pending": tele.login_code_pending,
        }
    )


async def handle_telethon_send_code(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Geçersiz JSON"}, status=400)
    phone = (data.get("phone") or "").strip()
    tele = request.app["tele"]
    result = await tele.login_send_code(phone)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def handle_telethon_sign_in(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Geçersiz JSON"}, status=400)
    code = (data.get("code") or "").strip()
    tele = request.app["tele"]
    result = await tele.login_submit_code(code)
    if result.get("ok") and result.get("need_password"):
        return web.json_response(result, status=200)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def handle_telethon_password(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Geçersiz JSON"}, status=400)
    password = (data.get("password") or "").strip()
    tele = request.app["tele"]
    result = await tele.login_submit_password(password)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def handle_mailforwarder_status(request: web.Request) -> web.Response:
    mf = request.app.get("mail_forwarder")
    if mf is None:
        return web.json_response({"ok": False, "error": "Mail forwarder yüklü değil"}, status=500)
    return web.json_response(mf.status_public())


async def handle_mailforwarder_toggle(request: web.Request) -> web.Response:
    mf = request.app.get("mail_forwarder")
    if mf is None:
        return web.json_response({"ok": False, "error": "Mail forwarder yüklü değil"}, status=500)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Geçersiz JSON"}, status=400)
    paused = bool(data.get("paused"))
    mf.set_paused(paused)
    return web.json_response({"ok": True, "paused": paused})


async def handle_mailforwarder_check_once(request: web.Request) -> web.Response:
    mf = request.app.get("mail_forwarder")
    if mf is None:
        return web.json_response({"ok": False, "error": "Mail forwarder yüklü değil"}, status=500)
    if not mf.configured():
        return web.json_response({"ok": False, "error": "Mail forwarder yapılandırması eksik"}, status=400)
    await asyncio.to_thread(mf.check_mail_once)
    return web.json_response(mf.status_public())


async def handle_mailforwarder_settings_get(request: web.Request) -> web.Response:
    mf = request.app.get("mail_forwarder")
    if mf is None:
        return web.json_response({"ok": False, "error": "Mail forwarder yüklü değil"}, status=500)
    return web.json_response(mf.settings_for_panel())


async def handle_mailforwarder_settings_save(request: web.Request) -> web.Response:
    mf = request.app.get("mail_forwarder")
    if mf is None:
        return web.json_response({"ok": False, "error": "Mail forwarder yüklü değil"}, status=500)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Geçersiz JSON"}, status=400)
    ok, err = mf.save_from_panel(data if isinstance(data, dict) else {})
    if not ok:
        return web.json_response({"ok": False, "error": err}, status=400)
    return web.json_response({"ok": True})


async def handle_admin_tokens_get(request: web.Request) -> web.Response:
    root: Path = request.app["root"]
    from panel_bot_settings import panel_bot_status

    base = panel_bot_status(root)
    mail_extra: dict[str, Any] = {
        "mail_telegram_bot_token_set": False,
        "mail_forward_chat_id": "",
    }
    mf = request.app.get("mail_forwarder")
    if mf is not None:
        ms = mf.settings_for_panel()
        if isinstance(ms, dict):
            mail_extra["mail_telegram_bot_token_set"] = bool(ms.get("telegram_bot_token_set"))
            fc = ms.get("forward_chat_id")
            mail_extra["mail_forward_chat_id"] = fc if fc is not None else ""

    return web.json_response({"ok": True, **base, **mail_extra})


async def handle_admin_tokens_save(request: web.Request) -> web.Response:
    root: Path = request.app["root"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Geçersiz JSON"}, status=400)
    if not isinstance(data, dict):
        data = {}

    from panel_bot_settings import revert_panel_bot_token, write_panel_bot_token

    warnings: list[str] = []

    if data.get("revert_group_bot_to_env"):
        ok, err = revert_panel_bot_token(root)
        if not ok:
            return web.json_response({"ok": False, "error": err}, status=400)
        warnings.append(
            "Grup botu için panel dosyası kaldırıldı; süreç yeniden başlatılınca BOT_TOKEN ortam değişkeni kullanılır."
        )

    gt = data.get("group_bot_token")
    if gt is not None and str(gt).strip():
        ok, err = write_panel_bot_token(root, str(gt))
        if not ok:
            return web.json_response({"ok": False, "error": err}, status=400)
        warnings.append(
            "Grup yönetimi bot tokenı kaydedildi. Ana botun yeni token ile çalışması için süreci yeniden başlatın."
        )

    mt = data.get("mail_telegram_bot_token")
    mf = request.app.get("mail_forwarder")
    if mf is not None and mt is not None and str(mt).strip():
        ok, err = mf.save_from_panel({"telegram_bot_token": str(mt).strip()})
        if not ok:
            return web.json_response({"ok": False, "error": err}, status=400)
        warnings.append("Mail iletim bot tokenı kaydedildi.")

    return web.json_response({"ok": True, "warnings": warnings})


def create_internal_app(
    registry: Any,
    tele: Any,
    bot: Any,
    internal_token: str,
    mail_forwarder: Any | None = None,
    root: Path | None = None,
) -> web.Application:
    app = web.Application(middlewares=[bearer_auth])
    app["registry"] = registry
    app["tele"] = tele
    app["bot"] = bot
    app["internal_token"] = internal_token
    app["mail_forwarder"] = mail_forwarder
    app["root"] = root if root is not None else Path(".")
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/joint", handle_joint)
    app.router.add_post("/api/refresh", handle_refresh)
    app.router.add_post("/api/invite", handle_invite)
    app.router.add_get("/api/invite/recipients", handle_invite_recipients)
    app.router.add_post("/api/invite/revoke", handle_invite_revoke)
    app.router.add_post("/api/kick/preview", handle_kick_preview)
    app.router.add_post("/api/kick", handle_kick)
    app.router.add_get("/api/telethon/status", handle_telethon_status)
    app.router.add_post("/api/telethon/send_code", handle_telethon_send_code)
    app.router.add_post("/api/telethon/sign_in", handle_telethon_sign_in)
    app.router.add_post("/api/telethon/password", handle_telethon_password)
    app.router.add_get("/api/mailforwarder/status", handle_mailforwarder_status)
    app.router.add_post("/api/mailforwarder/toggle", handle_mailforwarder_toggle)
    app.router.add_post("/api/mailforwarder/check-once", handle_mailforwarder_check_once)
    app.router.add_get("/api/mailforwarder/settings", handle_mailforwarder_settings_get)
    app.router.add_post("/api/mailforwarder/settings", handle_mailforwarder_settings_save)
    app.router.add_get("/api/admin/tokens", handle_admin_tokens_get)
    app.router.add_post("/api/admin/tokens", handle_admin_tokens_save)
    return app


async def start_internal_api(
    registry: Any,
    tele: Any,
    bot: Any,
    host: str,
    port: int,
    token: str,
    mail_forwarder: Any | None = None,
    root: Path | None = None,
) -> web.AppRunner | None:
    if not token:
        log.warning("INTERNAL_PANEL_TOKEN boş — iç API başlatılmadı.")
        return None
    app = create_internal_app(
        registry, tele, bot, token, mail_forwarder=mail_forwarder, root=root
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("İç API dinleniyor: http://%s:%s", host, port)
    return runner

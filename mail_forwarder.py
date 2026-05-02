"""
IMAP → Telegram iletim. Yapılandırma: panelden kayıtlı dosya (mail_forwarder_config.json)
veya ortam değişkenleri. Telegram için mail'e özel bot token kullanılır (ana BOT_TOKEN ile karışmaz).
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
import logging
import mimetypes
import os
import tempfile
import time
from dataclasses import dataclass
from email.header import decode_header
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)


def decode_mime_words(s: str | None) -> str:
    if not s:
        return ""
    decoded = decode_header(s)
    return "".join(
        str(t[0], t[1] or "utf-8") if isinstance(t[0], bytes) else t[0] for t in decoded
    )


def parse_sender(sender_raw: str | None) -> tuple[str, str]:
    name, sender_email = parseaddr(sender_raw or "")
    decoded_name = decode_mime_words(name).strip()
    return decoded_name, sender_email.strip()


def safe_filename(name: str | None) -> str:
    cleaned = (name or "").strip().replace("\r", "").replace("\n", "")
    for ch in '\\/:*?"<>|':
        cleaned = cleaned.replace(ch, "_")
    return cleaned or "file"


def guess_extension_from_type(content_type: str | None) -> str:
    ext = mimetypes.guess_extension(content_type or "")
    return ext if ext else ".bin"


def detect_telegram_media_type(file_path: str) -> str:
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type:
        if mime_type.startswith("image/"):
            return "photo"
        if mime_type.startswith("video/"):
            return "video"
    return "document"


def ensure_download_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def cleanup_download_folder(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


@dataclass
class MailForwarderState:
    initialized: bool = False
    last_uid: int = 0
    runtime_paused: bool = False
    last_error: str | None = None
    last_check_at: float | None = None


class MailForwarderManager:
    """IMAP ↔ Telegram; panel ayarları dosyada saklanır."""

    def __init__(self, settings: Any, root: Path) -> None:
        self._settings = settings
        self._root = root
        self._config_path = root / "mail_forwarder_config.json"
        self._download_dir = root / "mail_attachments"
        self.state = MailForwarderState()
        self._disk: dict[str, Any] = {}
        self.reload_disk()

    def reload_disk(self) -> None:
        self._disk = {}
        if not self._config_path.is_file():
            return
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._disk = raw
        except Exception as e:
            log.warning("mail_forwarder_config.json okunamadı: %s", e)
            self._disk = {}

    def _effective(self) -> dict[str, Any]:
        """Dosya + ortam birleşimi. Mail Telegram token yalnızca dosya / MAIL_TELEGRAM_BOT_TOKEN."""
        s = self._settings
        d = dict(self._disk)

        has_file = self._config_path.is_file()
        if has_file:
            enabled = bool(d.get("enabled", False))
        else:
            enabled = bool(getattr(s, "mail_forwarder_enabled", False))

        imap_host = (d.get("imap_host") or getattr(s, "mail_imap_host", "") or "").strip()
        imap_user = (d.get("imap_user") or getattr(s, "mail_imap_user", "") or "").strip()
        pw = d.get("imap_password")
        if pw is None or str(pw).strip() == "":
            imap_password = (getattr(s, "mail_imap_password", "") or "").strip()
        else:
            imap_password = str(pw).strip()

        tg = (d.get("telegram_bot_token") or os.environ.get("MAIL_TELEGRAM_BOT_TOKEN", "") or "").strip()

        fc = d.get("forward_chat_id")
        if fc is None or fc == "":
            forward_chat_id = getattr(s, "mail_forward_chat_id", None)
        else:
            try:
                forward_chat_id = int(fc)
            except (TypeError, ValueError):
                forward_chat_id = getattr(s, "mail_forward_chat_id", None)

        pi = d.get("poll_interval_sec")
        if pi is None:
            poll_sec = max(10, int(getattr(s, "mail_poll_interval_sec", 10)))
        else:
            try:
                poll_sec = max(10, int(pi))
            except (TypeError, ValueError):
                poll_sec = max(10, int(getattr(s, "mail_poll_interval_sec", 10)))

        return {
            "enabled": enabled,
            "imap_host": imap_host,
            "imap_user": imap_user,
            "imap_password": imap_password,
            "telegram_bot_token": tg,
            "forward_chat_id": forward_chat_id,
            "poll_interval_sec": poll_sec,
            "has_panel_file": has_file,
        }

    def configured(self) -> bool:
        e = self._effective()
        if not e["enabled"]:
            return False
        if not e["telegram_bot_token"]:
            return False
        if not e["imap_host"] or not e["imap_user"] or not e["imap_password"]:
            return False
        if e["forward_chat_id"] is None:
            return False
        return True

    def status_public(self) -> dict[str, Any]:
        e = self._effective()
        host = e["imap_host"]
        return {
            "ok": True,
            "configured": self.configured(),
            "paused": self.state.runtime_paused,
            "initialized": self.state.initialized,
            "last_uid": self.state.last_uid,
            "last_error": self.state.last_error,
            "last_check_at": self.state.last_check_at,
            "poll_interval_sec": e["poll_interval_sec"],
            "imap_host": host,
            "enabled": e["enabled"],
            "settings_source": "panel" if e["has_panel_file"] else "env",
        }

    def settings_for_panel(self) -> dict[str, Any]:
        """Form için; sırlar maslenir."""
        e = self._effective()
        pw_set = bool(self._disk.get("imap_password")) or bool(
            (getattr(self._settings, "mail_imap_password", "") or "").strip()
        )
        tok_set = bool(self._disk.get("telegram_bot_token")) or bool(
            os.environ.get("MAIL_TELEGRAM_BOT_TOKEN", "").strip()
        )
        fc = e["forward_chat_id"]
        return {
            "ok": True,
            "enabled": e["enabled"],
            "imap_host": e["imap_host"],
            "imap_user": e["imap_user"],
            "imap_password_set": pw_set,
            "telegram_bot_token_set": tok_set,
            "forward_chat_id": fc if fc is not None else "",
            "poll_interval_sec": e["poll_interval_sec"],
            "has_panel_file": e["has_panel_file"],
        }

    def save_from_panel(self, body: dict[str, Any]) -> tuple[bool, str]:
        from env_sanitize import looks_like_bot_token, sanitize_bot_token

        cur = dict(self._disk)
        if "enabled" in body:
            cur["enabled"] = bool(body["enabled"])

        if body.get("imap_host") is not None:
            cur["imap_host"] = str(body.get("imap_host") or "").strip()
        if body.get("imap_user") is not None:
            cur["imap_user"] = str(body.get("imap_user") or "").strip()

        pwd_in = body.get("imap_password")
        if pwd_in is not None and str(pwd_in).strip() != "":
            cur["imap_password"] = str(pwd_in).strip()

        tok_in = body.get("telegram_bot_token")
        if tok_in is not None and str(tok_in).strip() != "":
            t = sanitize_bot_token(str(tok_in).strip())
            if not t or not looks_like_bot_token(t):
                return False, "Telegram bot token geçersiz görünüyor"
            cur["telegram_bot_token"] = t

        if body.get("forward_chat_id") is not None:
            raw = str(body.get("forward_chat_id") or "").strip()
            if raw:
                try:
                    cur["forward_chat_id"] = int(raw)
                except ValueError:
                    return False, "MAIL_FORWARD_CHAT_ID geçerli bir tam sayı olmalı"
            else:
                cur.pop("forward_chat_id", None)

        if body.get("poll_interval_sec") is not None:
            try:
                cur["poll_interval_sec"] = max(10, int(body.get("poll_interval_sec")))
            except (TypeError, ValueError):
                return False, "poll_interval_sec geçerli bir tam sayı olmalı"

        if cur.get("enabled"):
            if not (cur.get("imap_host") or "").strip():
                return False, "IMAP sunucu adresi gerekli"
            if not (cur.get("imap_user") or "").strip():
                return False, "IMAP kullanıcı adı gerekli"
            pw_ok = bool((cur.get("imap_password") or "").strip()) or bool(
                (getattr(self._settings, "mail_imap_password", "") or "").strip()
            )
            if not pw_ok:
                return False, "IMAP şifresi gerekli"
            tg_ok = bool((cur.get("telegram_bot_token") or "").strip()) or bool(
                os.environ.get("MAIL_TELEGRAM_BOT_TOKEN", "").strip()
            )
            if not tg_ok:
                return False, "Mail bildirimleri için ayrı Telegram bot token gerekli"
            fc = cur.get("forward_chat_id")
            if fc is None:
                fc = getattr(self._settings, "mail_forward_chat_id", None)
            if fc is None:
                return False, "Telegram hedef sohbet / kullanıcı ID gerekli"

        try:
            fd, tmp = tempfile.mkstemp(dir=str(self._root), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._config_path)
        except OSError as e:
            return False, str(e)

        self.reload_disk()
        log.info("Mail forwarder ayarları kaydedildi: %s", self._config_path)
        return True, ""

    def set_paused(self, paused: bool) -> None:
        self.state.runtime_paused = paused

    def _tg_token(self) -> str:
        return self._effective()["telegram_bot_token"]

    def _tg_chat(self) -> int | None:
        cid = self._effective()["forward_chat_id"]
        return int(cid) if cid is not None else None

    def _send_message(self, text: str) -> None:
        token = self._tg_token()
        chat_id = self._tg_chat()
        if not token or chat_id is None:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=60)

    def _send_file(self, file_path: str, caption: str | None = None) -> None:
        token = self._tg_token()
        chat_id = self._tg_chat()
        if not token or chat_id is None:
            return
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        with open(file_path, "rb") as f:
            requests.post(url, data=data, files={"document": f}, timeout=120)

    def _send_single_media(self, file_path: str, caption: str | None = None) -> None:
        token = self._tg_token()
        chat_id = self._tg_chat()
        if not token or chat_id is None:
            return
        media_type = detect_telegram_media_type(file_path)
        if media_type == "photo":
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            field_name = "photo"
        elif media_type == "video":
            url = f"https://api.telegram.org/bot{token}/sendVideo"
            field_name = "video"
        else:
            self._send_file(file_path, caption=caption)
            return
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        with open(file_path, "rb") as f:
            requests.post(url, data=data, files={field_name: f}, timeout=120)

    def _send_media_group(self, file_paths: list[str], caption: str) -> None:
        token = self._tg_token()
        chat_id = self._tg_chat()
        if not token or chat_id is None:
            return
        chunks = [file_paths[i : i + 10] for i in range(0, len(file_paths), 10)]
        for chunk_index, chunk in enumerate(chunks):
            if len(chunk) == 1:
                single_caption = caption if chunk_index == 0 else None
                self._send_single_media(chunk[0], caption=single_caption)
                continue
            url = f"https://api.telegram.org/bot{token}/sendMediaGroup"
            media = []
            files_open: dict[str, Any] = {}
            for idx, path in enumerate(chunk):
                attach_name = f"file{idx}"
                media_type = detect_telegram_media_type(path)
                item: dict[str, Any] = {"type": media_type, "media": f"attach://{attach_name}"}
                if idx == 0 and chunk_index == 0:
                    item["caption"] = caption[:1024]
                media.append(item)
                files_open[attach_name] = open(path, "rb")
            try:
                requests.post(
                    url,
                    data={"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
                    files=files_open,
                    timeout=120,
                )
            finally:
                for fh in files_open.values():
                    fh.close()

    def _process_uid(self, mail: imaplib.IMAP4_SSL, uid: bytes | str) -> bool:
        uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
        result, msg_data = mail.uid("fetch", uid_str, "(RFC822)")
        if result != "OK" or not msg_data or not msg_data[0]:
            return False

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_words(msg.get("subject")).strip() or "(Konu yok)"
        sender_name, sender_email = parse_sender(msg.get("From") or msg.get("from"))

        body = ""
        attachment_paths: list[str] = []

        ensure_download_folder(self._download_dir)

        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition") or "")
            content_disposition_lower = content_disposition.lower()
            maintype = part.get_content_maintype()

            if content_type == "text/plain" and "attachment" not in content_disposition_lower:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors="ignore")

            is_attachment = "attachment" in content_disposition_lower
            is_inline_file = "inline" in content_disposition_lower and (
                part.get_filename() or maintype in ("image", "video", "audio", "application")
            )
            has_named_file = bool(part.get_filename())
            if is_attachment or is_inline_file or has_named_file:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue

                filename = part.get_filename()
                if filename:
                    filename = decode_mime_words(filename)
                else:
                    content_id = (part.get("Content-ID") or "").strip("<>")
                    suffix = guess_extension_from_type(content_type)
                    filename = f"inline_{content_id or len(attachment_paths) + 1}{suffix}"

                filename = safe_filename(filename)
                filepath = self._download_dir / filename
                with open(filepath, "wb") as f:
                    f.write(payload)
                attachment_paths.append(str(filepath))

        sender_line = sender_name if sender_name else "(İsim yok)"
        email_line = sender_email if sender_email else "(E-posta yok)"
        body_preview = body.strip()[:500] if body else "(Mesaj içeriği yok)"

        text = (
            "📩 Yeni Mail\n"
            f"👤 İsim: {sender_line}\n"
            f"📧 E-posta: {email_line}\n"
            f"📝 Konu: {subject}\n\n"
            f"{body_preview}"
        )

        try:
            if attachment_paths:
                self._send_media_group(attachment_paths, text)
            else:
                self._send_message(text)
        finally:
            for path in attachment_paths:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
            cleanup_download_folder(self._download_dir)

        return True

    def check_mail_once(self) -> None:
        if not self.configured():
            return
        e = self._effective()
        self.state.last_check_at = time.time()
        self.state.last_error = None

        try:
            mail = imaplib.IMAP4_SSL(e["imap_host"])
            mail.login(e["imap_user"], e["imap_password"])
            mail.select("inbox")

            result, data = mail.uid("search", None, "ALL")
            if result != "OK":
                mail.logout()
                return

            all_uids = data[0].split() if data and data[0] else []
            if not all_uids:
                self.state.initialized = True
                self.state.last_uid = 0
                mail.logout()
                return

            st = self.state
            if not st.initialized:
                last_uid = all_uids[-1]
                if self._process_uid(mail, last_uid):
                    st.last_uid = int(last_uid.decode())
                st.initialized = True
                mail.logout()
                return

            new_uids = [u for u in all_uids if int(u.decode()) > st.last_uid]
            for uid in new_uids:
                if self._process_uid(mail, uid):
                    st.last_uid = int(uid.decode())

            mail.logout()
        except Exception as ex:
            err = str(ex)
            self.state.last_error = err
            log.warning("Mail forwarder: %s", err)
            try:
                self._send_message(f"❌ Mail forwarder hatası: {err}")
            except Exception:
                log.exception("Telegram üzerinden hata iletilemedi")

    async def run_loop(self) -> None:
        while True:
            e = self._effective()
            interval = max(10, int(e.get("poll_interval_sec", 10)))
            try:
                if self.configured() and not self.state.runtime_paused:
                    await asyncio.to_thread(self.check_mail_once)
                else:
                    await asyncio.sleep(min(interval, 30))
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Mail forwarder döngü hatası")
            await asyncio.sleep(interval)

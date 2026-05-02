"""
.env / terminal girişlerinden gelen görünmez (C0/C1) karakterleri temizler.
Windows'ta getpass veya pano bazen SYN vb. karakter ekler; httpx URL hatasına yol açar.
"""
from __future__ import annotations

import re


def strip_invisible_ascii(value: str) -> str:
    if not value:
        return ""
    v = value.replace("\ufeff", "")
    return "".join(ch for ch in v if 32 <= ord(ch) < 127)


def sanitize_bot_token(token: str) -> str:
    t = strip_invisible_ascii(token)
    t = "".join(ch for ch in t if not ch.isspace())
    return t


def sanitize_api_hash(api_hash: str) -> str:
    return strip_invisible_ascii(api_hash).replace(" ", "")


def sanitize_api_id(api_id: str) -> str:
    return "".join(ch for ch in strip_invisible_ascii(api_id) if ch.isdigit())


def sanitize_session_name(name: str) -> str:
    t = strip_invisible_ascii(name)
    t = re.sub(r"[^\w\-]", "", t)
    return t or "user_session"


def looks_like_bot_token(token: str) -> bool:
    if ":" not in token:
        return False
    left, _, right = token.partition(":")
    if not left.isdigit() or len(right) < 15:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", right))

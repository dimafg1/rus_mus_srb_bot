from __future__ import annotations
from dataclasses import dataclass
import base64
import hmac
import hashlib
import os
import time
import secrets

VERSION = "v1"
ENV_KEY = "RUS_MUS_SRB_BOT_SIGNING_KEY"


@dataclass
class AuthContext:
    listing_id: int
    owner_id: int
    exp: int
    purpose: str
    jti: str


@dataclass
class ContactClickContext:
    listing_id: int
    user_id: int
    source: str
    exp: int
    purpose: str
    jti: str


def _get_secret() -> bytes:
    key = os.environ.get(ENV_KEY)
    if not key:
        raise RuntimeError(
            f"Signing key is missing. Set env {ENV_KEY} "
            f"to a long random string (e.g., 64 hex chars)."
        )
    return key.encode("utf-8")


def _b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign_token(
    listing_id: int,
    owner_id: int,
    ttl_seconds: int = 3600,
    purpose: str = "editor",
    jti: str | None = None,
) -> str:
    """
    Создать токен для URL: unixound.com/.../editor?listing=...&t=<token>
    purpose: "editor" (для страницы) или "media" (для медиа-прокси).
    """
    if jti is None:
        jti = secrets.token_hex(8)
    exp = int(time.time()) + int(ttl_seconds)
    payload_str = f"{int(listing_id)}|{int(owner_id)}|{exp}|{purpose}|{jti}"
    payload_b64 = _b64u_encode(payload_str.encode("utf-8"))
    secret = _get_secret()
    sig = hmac.new(secret, f"{VERSION}.{payload_b64}".encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _b64u_encode(sig)
    return f"{VERSION}.{payload_b64}.{sig_b64}"


def verify_token(token: str, purpose: str | None = None) -> AuthContext:
    """
    Проверить токен. Если задан purpose — дополнительно сверяется назначение.
    Бросает ValueError при любой ошибке/просрочке.
    """
    if not token or token.count(".") != 2:
        raise ValueError("Bad token format")
    version, payload_b64, sig_b64 = token.split(".", 2)
    if version != VERSION:
        raise ValueError("Unsupported token version")

    secret = _get_secret()
    expected_sig = hmac.new(secret, f"{version}.{payload_b64}".encode("utf-8"), hashlib.sha256).digest()
    got_sig = _b64u_decode(sig_b64)
    if not hmac.compare_digest(expected_sig, got_sig):
        raise ValueError("Bad signature")

    payload_raw = _b64u_decode(payload_b64).decode("utf-8", errors="strict")
    parts = payload_raw.split("|")
    if len(parts) != 5:
        raise ValueError("Bad payload")

    listing_id_s, owner_id_s, exp_s, purpose_s, jti = parts
    try:
        listing_id = int(listing_id_s)
        owner_id = int(owner_id_s)
        exp = int(exp_s)
    except Exception:
        raise ValueError("Bad payload ints")

    if time.time() > exp:
        raise ValueError("Token expired")
    if purpose is not None and purpose_s != purpose:
        raise ValueError("Wrong purpose")

    return AuthContext(
        listing_id=listing_id,
        owner_id=owner_id,
        exp=exp,
        purpose=purpose_s,
        jti=jti,
    )


def sign_contact_click_token(
    listing_id: int,
    user_id: int,
    source: str,
    ttl_seconds: int = 3600,
    purpose: str = "contact_click",
    jti: str | None = None,
) -> str:
    """
    Токен для 1-click redirect на контакт продавца.
    Хранит:
    - listing_id
    - user_id того, кто кликает
    - source (search/catalog/my/direct)
    """
    if jti is None:
        jti = secrets.token_hex(8)

    exp = int(time.time()) + int(ttl_seconds)
    source_clean = (source or "direct").strip()

    payload_str = f"{int(listing_id)}|{int(user_id)}|{source_clean}|{exp}|{purpose}|{jti}"
    payload_b64 = _b64u_encode(payload_str.encode("utf-8"))

    secret = _get_secret()
    sig = hmac.new(secret, f"{VERSION}.{payload_b64}".encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _b64u_encode(sig)

    return f"{VERSION}.{payload_b64}.{sig_b64}"


def verify_contact_click_token(
    token: str,
    purpose: str = "contact_click",
) -> ContactClickContext:
    """
    Проверка токена редиректа контакта.
    """
    if not token or token.count(".") != 2:
        raise ValueError("Bad token format")

    version, payload_b64, sig_b64 = token.split(".", 2)
    if version != VERSION:
        raise ValueError("Unsupported token version")

    secret = _get_secret()
    expected_sig = hmac.new(secret, f"{version}.{payload_b64}".encode("utf-8"), hashlib.sha256).digest()
    got_sig = _b64u_decode(sig_b64)
    if not hmac.compare_digest(expected_sig, got_sig):
        raise ValueError("Bad signature")

    payload_raw = _b64u_decode(payload_b64).decode("utf-8", errors="strict")
    parts = payload_raw.split("|")
    if len(parts) != 6:
        raise ValueError("Bad payload")

    listing_id_s, user_id_s, source_s, exp_s, purpose_s, jti = parts

    try:
        listing_id = int(listing_id_s)
        user_id = int(user_id_s)
        exp = int(exp_s)
    except Exception:
        raise ValueError("Bad payload ints")

    if time.time() > exp:
        raise ValueError("Token expired")
    if purpose_s != purpose:
        raise ValueError("Wrong purpose")

    return ContactClickContext(
        listing_id=listing_id,
        user_id=user_id,
        source=source_s,
        exp=exp,
        purpose=purpose_s,
        jti=jti,
    )
"""Two-factor sign-in: authenticator apps (TOTP), recovery codes, and a lockout after
repeated failures.

Everything is optional and set up in Settings -> Security. Once the authenticator app is
on, signing in needs the password plus a 6-digit code (or a one-time recovery code).
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from urllib.parse import quote

import segno
from fastapi import HTTPException, Request

from . import db
from .config import settings

TOTP_KEY = "auth.totp_secret"
TOTP_PENDING_KEY = "auth.totp_pending"
TOTP_LAST_STEP_KEY = "auth.totp_last_step"
RECOVERY_KEY = "auth.recovery_hashes"


# ---------- lockout ----------

MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
_failures: dict[str, list[float]] = {}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def check_lock(request: Request) -> None:
    ip = client_ip(request)
    cutoff = time.time() - LOCK_SECONDS
    for other in [k for k, times in _failures.items() if not times or times[-1] < cutoff]:
        del _failures[other]   # forget addresses with no recent failures
    recent = [t for t in _failures.get(ip, []) if t > cutoff]
    if recent:
        _failures[ip] = recent
    if len(recent) >= MAX_FAILURES:
        minutes = int((recent[0] + LOCK_SECONDS - time.time()) // 60) + 1
        raise HTTPException(429, f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.")


def record_failure(request: Request) -> None:
    _failures.setdefault(client_ip(request), []).append(time.time())


def clear_failures(request: Request) -> None:
    _failures.pop(client_ip(request), None)


# ---------- state ----------

def totp_enabled() -> bool:
    return bool(db.get_setting(TOTP_KEY))


def second_factor_enabled() -> bool:
    return totp_enabled()


def summary() -> dict:
    return {
        "totp": totp_enabled(),
        "totp_pending": not totp_enabled() and bool(db.get_setting(TOTP_PENDING_KEY)),
        "recovery_left": len(json.loads(db.get_setting(RECOVERY_KEY, "[]"))),
    }


# ---------- recovery codes ----------

def _hash_code(code: str) -> str:
    return hashlib.sha256(code.lower().replace("-", "").replace(" ", "").encode()).hexdigest()


def new_recovery_codes() -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"   # no look-alike characters
    codes = ["".join(secrets.choice(alphabet) for _ in range(8)) for _ in range(10)]
    codes = [f"{c[:4]}-{c[4:]}" for c in codes]
    db.set_setting(RECOVERY_KEY, json.dumps([_hash_code(c) for c in codes]))
    return codes


def use_recovery_code(code: str) -> bool:
    hashes = json.loads(db.get_setting(RECOVERY_KEY, "[]"))
    h = _hash_code(code)
    if h not in hashes:
        return False
    hashes.remove(h)
    db.set_setting(RECOVERY_KEY, json.dumps(hashes))
    return True


# ---------- TOTP (authenticator apps, RFC 6238) ----------

def _totp(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10**6:06d}"


def _totp_match(secret: str, code: str, last_step: int = -1) -> int | None:
    """Return the matching time step (±30 s allowed for clock drift), or None.
    Steps at or before last_step are refused so a code can't be replayed."""
    code = code.replace(" ", "")
    now = int(time.time() // 30)
    for step in (now - 1, now, now + 1):
        if step > last_step and hmac.compare_digest(_totp(secret, step), code):
            return step
    return None


def totp_start(new_key: bool = False) -> dict:
    """Begin (or resume) authenticator setup. An unfinished setup keeps its key, so a code
    already scanned into the app still works when you come back to finish."""
    secret = db.get_setting(TOTP_PENDING_KEY)
    if new_key or not secret:
        secret = base64.b32encode(os.urandom(20)).decode().rstrip("=")
        db.set_setting(TOTP_PENDING_KEY, secret)
    label = quote(f"{settings.app_name}:owner")
    uri = f"otpauth://totp/{label}?secret={secret}&issuer={quote(settings.app_name)}&digits=6&period=30"
    # omitsize gives the SVG a viewBox, so it scales to its box instead of being cropped;
    # border=4 is the standard quiet zone phones need to find the code.
    qr = segno.make(uri, error="m").svg_inline(border=4, omitsize=True, dark="#000000", light="#ffffff")
    return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), "qr_svg": qr}


def totp_confirm(code: str) -> list[str]:
    """Turn the authenticator on. Returns fresh recovery codes to show once."""
    secret = db.get_setting(TOTP_PENDING_KEY)
    if not secret:
        raise HTTPException(400, "Start the setup again.")
    step = _totp_match(secret, code)
    if step is None:
        raise HTTPException(400, "That code didn't match. Check your phone's clock and try the newest code.")
    db.set_setting(TOTP_KEY, secret)
    db.set_setting(TOTP_LAST_STEP_KEY, str(step))
    db.delete_setting(TOTP_PENDING_KEY)
    return new_recovery_codes()


def totp_disable() -> None:
    for k in (TOTP_KEY, TOTP_LAST_STEP_KEY, RECOVERY_KEY):
        db.delete_setting(k)


def totp_verify(code: str) -> bool:
    secret = db.get_setting(TOTP_KEY)
    if not secret:
        return False
    step = _totp_match(secret, code, int(db.get_setting(TOTP_LAST_STEP_KEY, "-1")))
    if step is None:
        return False
    db.set_setting(TOTP_LAST_STEP_KEY, str(step))
    return True


def reset_all() -> None:
    """Emergency switch (scripts/reset_2fa.py): turn two-factor sign-in off."""
    for k in (TOTP_KEY, TOTP_PENDING_KEY, TOTP_LAST_STEP_KEY, RECOVERY_KEY,
              # left over from the removed security-key feature
              "auth.webauthn_keys", "auth.webauthn_user", "auth.key_only_login"):
        db.delete_setting(k)

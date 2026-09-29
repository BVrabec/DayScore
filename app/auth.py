"""Single-user password login with a signed, long-lived cookie.

The password is created in the browser on first visit (stored as a salted hash), or
fixed with the APP_PASSWORD environment variable.
"""

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from . import db
from .config import settings

COOKIE_NAME = "dayscore_session"
SESSION_SECONDS = 90 * 24 * 3600
HASH_KEY = "auth.password_hash"
EPOCH_KEY = "auth.session_epoch"
ITERATIONS = 600_000


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def password_configured() -> bool:
    return bool(db.get_setting(HASH_KEY) or settings.app_password)


def set_password(password: str) -> None:
    salt = secrets.token_hex(16)
    db.set_setting(HASH_KEY, f"{salt}${_hash(password, salt)}")


def check_password(password: str) -> bool:
    stored = db.get_setting(HASH_KEY)
    if stored:
        salt, _, digest = stored.partition("$")
        return hmac.compare_digest(_hash(password, salt), digest)
    return bool(settings.app_password) and hmac.compare_digest(
        password.encode(), settings.app_password.encode()
    )


def _secret() -> bytes:
    """Signing key: a random file in the data folder, mixed with the current password and a
    session epoch, so changing either logs out every other device."""
    path = settings.data_dir / "secret.key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    material = (db.get_setting(HASH_KEY) or settings.app_password) + db.get_setting(EPOCH_KEY, "")
    return hashlib.sha256((path.read_text().strip() + material).encode()).digest()


def end_other_sessions() -> None:
    """Invalidate every session cookie; the caller re-issues one for the current device."""
    db.set_setting(EPOCH_KEY, secrets.token_hex(8))


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_session() -> str:
    expires = str(int(time.time()) + SESSION_SECONDS)
    return f"{expires}.{_sign(expires)}"


PENDING_COOKIE = "dayscore_pending"
PENDING_SECONDS = 5 * 60


def make_pending() -> str:
    """Proof that the password was right, while the second factor is still missing."""
    expires = str(int(time.time()) + PENDING_SECONDS)
    return f"{expires}.{_sign('pending:' + expires)}"


def is_pending(request: Request) -> bool:
    expires, _, signature = request.cookies.get(PENDING_COOKIE, "").partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(signature, _sign("pending:" + expires))


def is_logged_in(request: Request) -> bool:
    if settings.disable_auth:
        return True
    if not password_configured():
        return False
    value = request.cookies.get(COOKIE_NAME, "")
    expires, _, signature = value.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(signature, _sign(expires))


def require_login(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="Not logged in")

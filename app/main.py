"""Web server: JSON API + the dashboard. Also starts the Telegram bot in the background."""

import asyncio
import csv
import io
import json
import logging
import re
from html import escape
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, backup_targets, backups, db, prefs, scoring, security, service, telegram, todoist
from .config import settings
from .scoring import CATEGORIES, ScoringError
from .version import VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# The HTTP client logs full request URLs, and Telegram puts the bot token in the URL.
for noisy in ("httpx2", "httpx", "httpcore", "httpcore2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("dayscore")

STATIC = Path(__file__).parent / "static"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
FAIL_DELAY = 1.5   # seconds added to every wrong password or code, to slow down guessing


LOCKED: str | None = None   # why the data can't be opened (missing/wrong key), if so


@asynccontextmanager
async def lifespan(app: FastAPI):
    global LOCKED
    try:
        db.init()
    except db.Locked as e:
        LOCKED = str(e)
        log.error("DayScore can't open its data: %s", e)
        yield
        return
    log.info("DayScore %s", VERSION)
    if db.encrypted():
        (settings.data_dir / "secret.key").unlink(missing_ok=True)   # sessions are signed with the key now
    else:
        log.warning("DAYSCORE_KEY isn't set, so the data is stored unencrypted. See the README.")
    if not auth.password_configured() and not settings.disable_auth:
        log.warning("New install: open the web page and enter this setup code: %s", auth.setup_code())
    backups.startup()
    telegram.restart()
    backup_task = asyncio.create_task(backups.scheduler())
    yield
    telegram.stop()
    backup_task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
logged_in = Depends(auth.require_login)
unlocked = Depends(auth.require_unlock)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if LOCKED and not request.url.path.startswith("/static/"):
        if request.url.path.startswith("/api/") or request.url.path == "/healthz":
            response = JSONResponse({"detail": LOCKED}, status_code=503)
        else:
            response = HTMLResponse(_locked_page(LOCKED), status_code=503)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"                         # no embedding in other sites
    response.headers["Content-Security-Policy"] = CSP
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


def _locked_page(message: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(settings.app_name)}</title>
<link rel="stylesheet" href="/static/style.css"></head><body><div class="login-page"><div class="card login-card">
<h2>{escape(settings.app_name)} can't open its data</h2><p>{escape(message, quote=False)}</p></div></div></body></html>"""


def _versioned(html: str) -> str:
    """Add ?v=<file mtime> to asset URLs so browsers never run a stale app.js/style.css,
    and apply the color theme and app name."""
    for name in ("app.js", "style.css", "backups.js", "login.js", "theme.js"):
        version = int((STATIC / name).stat().st_mtime)
        html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
    if settings.accent == "green":
        html = html.replace('<html lang="en">', '<html lang="en" data-accent="green">', 1)
    return html.replace("<title>DayScore</title>", f"<title>{escape(settings.app_name)}</title>", 1)


def _session_response(body: dict) -> JSONResponse:
    resp = JSONResponse(body)
    resp.delete_cookie(auth.PENDING_COOKIE)
    resp.set_cookie(
        auth.COOKIE_NAME, auth.make_session(), max_age=auth.SESSION_SECONDS,
        httponly=True, samesite="strict", secure=settings.secure_cookies,
    )
    return resp


# ---------- pages ----------

@app.get("/", include_in_schema=False)
def index(request: Request):
    if not auth.is_logged_in(request):
        return RedirectResponse("/login")
    html = _versioned((STATIC / "index.html").read_text())
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if auth.is_logged_in(request):
        return RedirectResponse("/")
    html = _versioned((STATIC / "login.html").read_text())
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# ---------- auth ----------

class Password(BaseModel):
    password: str


async def _fail(request: Request, status: int, message: str):
    security.record_failure(request)
    await asyncio.sleep(FAIL_DELAY)
    raise HTTPException(status, message)


def _require_pending(request: Request) -> None:
    if not auth.is_pending(request):
        raise HTTPException(401, "Please enter your password again.")


@app.get("/api/auth")
def auth_status():
    return {
        "setup_needed": not auth.password_configured(),
        "app_name": settings.app_name,
    }


class Setup(BaseModel):
    password: str
    setup_code: str = ""


async def _check_setup_code(request: Request, code: str) -> None:
    """A new install can only be claimed with the code from the server logs."""
    security.check_lock(request)
    if not auth.check_setup_code(code):
        await _fail(request, 400, "The setup code isn't right. Find it in the server logs: docker compose logs dayscore")


@app.post("/api/setup")
async def first_setup(body: Setup, request: Request):
    """Create the password on first visit. Only works while no password exists."""
    if auth.password_configured():
        raise HTTPException(400, "A password is already set.")
    await _check_setup_code(request, body.setup_code)
    if len(body.password) < 8:
        raise HTTPException(400, "Use at least 8 characters.")
    auth.set_password(body.password)
    security.clear_failures(request)
    return _session_response({"ok": True})


@app.post("/api/login")
async def login(body: Password, request: Request):
    security.check_lock(request)
    if not auth.password_configured():
        raise HTTPException(400, "No password yet. Reload the page to create one.")
    if not await asyncio.to_thread(auth.check_password, body.password):
        await _fail(request, 401, "Wrong password.")
    if not security.second_factor_enabled():
        security.clear_failures(request)
        return _session_response({"ok": True})
    # Password was right; now a second factor is needed.
    resp = JSONResponse({"second_factor": {"totp": True}})
    resp.set_cookie(auth.PENDING_COOKIE, auth.make_pending(), max_age=auth.PENDING_SECONDS,
                    httponly=True, samesite="strict", secure=settings.secure_cookies)
    return resp


class Code(BaseModel):
    code: str


@app.post("/api/login/totp")
async def login_totp(body: Code, request: Request):
    security.check_lock(request)
    _require_pending(request)
    if not security.totp_verify(body.code):
        await _fail(request, 401, "That code isn't right. Use the newest code from your app.")
    security.clear_failures(request)
    return _session_response({"ok": True})


@app.post("/api/login/recovery")
async def login_recovery(body: Code, request: Request):
    security.check_lock(request)
    _require_pending(request)
    if not security.use_recovery_code(body.code):
        await _fail(request, 401, "That recovery code isn't valid (each one works only once).")
    security.clear_failures(request)
    log.warning("Signed in with a recovery code")
    return _session_response({"ok": True})


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    resp.delete_cookie(auth.UNLOCK_COOKIE)
    return resp


@app.post("/api/logout/all", dependencies=[logged_in])
def logout_everywhere():
    """Sign out every device, including this one (e.g. after losing a phone)."""
    auth.end_other_sessions()
    return logout()


class PasswordChange(BaseModel):
    current: str
    new: str


@app.put("/api/password", dependencies=[logged_in])
async def change_password(body: PasswordChange, request: Request):
    security.check_lock(request)
    if not await asyncio.to_thread(auth.check_password, body.current):
        await _fail(request, 400, "The current password is wrong.")
    if len(body.new) < 8:
        raise HTTPException(400, "Use at least 8 characters.")
    await asyncio.to_thread(auth.set_password, body.new)
    return _session_response({"ok": True})  # re-issue this device's cookie; others are logged out


# ---------- security settings (every change needs the password) ----------

class Confirm(BaseModel):
    password: str


async def _confirm(request: Request, password: str) -> None:
    security.check_lock(request)
    if not await asyncio.to_thread(auth.check_password, password):
        await _fail(request, 400, "The password is wrong.")


@app.post("/api/unlock", dependencies=[logged_in])
async def unlock(body: Confirm, request: Request):
    """Re-enter the password to change sensitive settings for the next 10 minutes."""
    await _confirm(request, body.password)
    resp = JSONResponse({"ok": True, "seconds": auth.UNLOCK_SECONDS})
    resp.set_cookie(auth.UNLOCK_COOKIE, auth.make_unlock(), max_age=auth.UNLOCK_SECONDS,
                    httponly=True, samesite="strict", secure=settings.secure_cookies)
    return resp


@app.get("/api/security", dependencies=[logged_in])
def security_status():
    return security.summary()


class TotpStart(BaseModel):
    password: str
    new_key: bool = False   # true = throw away an unfinished setup and make a new key


@app.post("/api/security/totp/start", dependencies=[logged_in])
async def totp_start(body: TotpStart, request: Request):
    await _confirm(request, body.password)
    return security.totp_start(body.new_key)


@app.post("/api/security/totp/confirm", dependencies=[logged_in])
def totp_confirm(body: Code):
    codes = security.totp_confirm(body.code)
    # Two-factor just turned on: sign every other device out, keep this one signed in.
    auth.end_other_sessions()
    return _session_response({**security.summary(), "recovery_codes": codes})


@app.post("/api/security/totp/disable", dependencies=[logged_in])
async def totp_disable(body: Confirm, request: Request):
    await _confirm(request, body.password)
    security.totp_disable()
    return security.summary()


@app.post("/api/security/recovery", dependencies=[logged_in])
async def regenerate_recovery(body: Confirm, request: Request):
    await _confirm(request, body.password)
    if not security.second_factor_enabled():
        raise HTTPException(400, "Turn on the authenticator app first.")
    return {**security.summary(), "recovery_codes": security.new_recovery_codes()}


# ---------- data ----------

def _config_summary() -> dict:
    token = prefs.get("telegram_token")
    return {
        "ai": {
            "provider": prefs.get("ai_provider"),
            "configured": prefs.ai_configured(),
            "providers": {
                name: {
                    "key_hint": prefs.mask(prefs.get(key_name)),
                    "model": prefs.get(model_name),
                    "models": prefs.MODELS[name],
                }
                for name, (key_name, model_name) in prefs.AI_KEYS.items()
            },
            "local_url": prefs.get("local_url"),
        },
        "telegram": {
            "token_set": bool(token),
            "token_hint": prefs.mask(token),
            "bot_username": prefs.get("telegram_bot_username"),
            "linked": prefs.telegram_user_id() is not None,
            "user_name": prefs.get("telegram_user_name"),
            "link_url": telegram.link_url(),
            "link_code": "" if prefs.telegram_user_id() is not None else prefs.get("telegram_link_code"),
            "status": telegram.status["state"],
            "status_detail": telegram.status["detail"],
        },
        "todoist": {
            "connected": bool(todoist.token()),
            "token_hint": prefs.mask(todoist.token()),
            "project_ids": todoist.project_ids(),
            "confirm": todoist.confirm_first(),
            "create": prefs.get("todoist_create") != "0",
        },
        "general": {
            "timezone": prefs.get("timezone"),
            "late_entry_until": prefs.late_entry_until().strftime("%H:%M"),
            "workdays": sorted(prefs.workdays()),
            "morning_reminder": prefs.get("morning_reminder"),
            "evening_reminder": prefs.get("evening_reminder"),
            "weekly_summary": prefs.get("weekly_summary") == "1",
        },
        "password_from_env": bool(settings.app_password) and not db.get_setting(auth.HASH_KEY),
        "security": {**security.summary(), "encrypted": db.encrypted()},
    }


def _by_day(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["day"], []).append(row)
    return grouped


@app.get("/api/state", dependencies=[logged_in])
def state():
    """Everything the dashboard needs in one call."""
    return {
        "app_name": settings.app_name,
        "version": VERSION,
        "today": service.today().isoformat(),
        "loggable_days": [d.isoformat() for d in service.loggable_days()],
        "default_day": service.default_day().isoformat(),
        "late_entry_until": prefs.late_entry_until().strftime("%H:%M"),
        "categories": CATEGORIES,
        "entries": db.list_entries(),
        "todoist_done": db.todoist_by_day(),
        "todoist_suggestions": _by_day(db.suggestions(status="pending")),
        "todoist_created": _by_day(db.created()),
        "stats": service.stats(),
        "priorities": db.get_setting("priorities", ""),
        "config": _config_summary(),
    }


class NewNote(BaseModel):
    day: date
    text: str
    mood: int | None = None


@app.post("/api/entries", dependencies=[logged_in])
async def add_note(body: NewNote):
    try:
        entry, previous, info = await service.log_day(body.day, body.text, source="web", mood=body.mood)
    except service.NotAllowed as e:
        raise HTTPException(400, str(e))
    except ScoringError as e:
        raise HTTPException(502, str(e))
    return {"entry": entry, "previous_score": previous, "todoist": info}


class EditNote(BaseModel):
    text: str


@app.put("/api/entries/{day}", dependencies=[logged_in])
async def edit_note(day: date, body: EditNote):
    try:
        entry, previous = await service.edit_day(day, body.text)
    except service.NotAllowed as e:
        raise HTTPException(400, str(e))
    except ScoringError as e:
        raise HTTPException(502, str(e))
    return {"entry": entry, "previous_score": previous}


@app.delete("/api/entries/{day}", dependencies=[logged_in])
async def delete_note(day: date):
    try:
        await service.delete_day(day)
    except service.NotAllowed as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


class Mood(BaseModel):
    mood: int | None = None


@app.put("/api/entries/{day}/mood", dependencies=[logged_in])
def save_mood(day: date, body: Mood):
    try:
        return {"entry": service.set_mood(day, body.mood)}
    except service.NotAllowed as e:
        raise HTTPException(400, str(e))


class Priorities(BaseModel):
    priorities: str


@app.put("/api/settings", dependencies=[logged_in])
def save_priorities(body: Priorities):
    db.set_setting("priorities", body.priorities.strip()[:4000])
    return {"ok": True}


# ---------- configuration from the Settings page ----------

class AIConfig(BaseModel):
    provider: str       # anthropic | openrouter | local
    api_key: str = ""   # empty = keep the saved key
    model: str
    url: str = ""       # local only: the OpenAI-compatible server


async def _save_local_ai(body: AIConfig) -> dict:
    url, model = body.url.strip(), body.model.strip()
    key = body.api_key.strip() or prefs.get("local_api_key")
    if not model:
        raise HTTPException(400, "Enter the model name, e.g. qwen3:8b.")
    try:
        available = await scoring.local_models(url, key)
    except ScoringError as e:
        raise HTTPException(400, str(e))
    if available and model not in available:
        raise HTTPException(400, f"That server doesn't have {model}. It has: {', '.join(available[:12])}")
    prefs.put("local_url", url)
    prefs.put("local_model", model)
    if body.api_key.strip():
        prefs.put("local_api_key", key)
    prefs.put("ai_provider", "local")
    return _config_summary()


@app.put("/api/config/ai", dependencies=[unlocked])
async def save_ai(body: AIConfig):
    if body.provider not in prefs.AI_KEYS:
        raise HTTPException(400, "Unknown provider.")
    if body.provider == "local":
        return await _save_local_ai(body)
    model = body.model.strip()
    # OpenRouter has hundreds of models, so any "vendor/model" ID is allowed there.
    if model not in prefs.MODELS[body.provider] and not (body.provider == "openrouter" and re.fullmatch(r"[\w.-]+/[\w.:-]+", model)):
        raise HTTPException(400, "Unknown model.")
    key_name, model_name = prefs.AI_KEYS[body.provider]
    key = body.api_key.strip()
    if key:
        try:
            await scoring.check_api_key(body.provider, key)
        except ScoringError as e:
            raise HTTPException(400, str(e))
        prefs.put(key_name, key)
    elif not prefs.get(key_name):
        raise HTTPException(400, "Paste your API key first.")
    prefs.put(model_name, model)
    prefs.put("ai_provider", body.provider)
    return _config_summary()


class TelegramConfig(BaseModel):
    token: str


@app.put("/api/config/telegram", dependencies=[unlocked])
async def save_telegram(body: TelegramConfig):
    token = body.token.strip()
    try:
        info = await telegram.check_token(token)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if token != prefs.get("telegram_token"):
        # A different bot: forget the old link so the new bot must be connected again.
        prefs.put("telegram_user_id", "")
        prefs.put("telegram_user_name", "")
    prefs.put("telegram_token", token)
    prefs.put("telegram_bot_username", info["username"])
    if prefs.telegram_user_id() is None:
        telegram.new_link_code()
    telegram.restart()
    return _config_summary()


@app.post("/api/config/telegram/unlink", dependencies=[unlocked])
def unlink_telegram():
    prefs.put("telegram_user_id", "")
    prefs.put("telegram_user_name", "")
    telegram.new_link_code()
    return _config_summary()


@app.delete("/api/config/telegram", dependencies=[logged_in])
def remove_telegram():
    for key in ("telegram_token", "telegram_user_id", "telegram_user_name",
                "telegram_bot_username", "telegram_link_code"):
        prefs.put(key, "")
    telegram.stop()
    return _config_summary()


class TodoistConfig(BaseModel):
    token: str


@app.put("/api/config/todoist", dependencies=[unlocked])
async def save_todoist(body: TodoistConfig):
    tok = body.token.strip()
    try:
        await todoist.fetch_projects(tok)  # validates the token
    except todoist.TodoistError as e:
        raise HTTPException(400, str(e))
    prefs.put("todoist_token", tok)
    return _config_summary()


class TodoistDecision(BaseModel):
    day: date
    accept: list[int] = []   # suggestion ids to tick off in Todoist
    reject: list[int] = []   # suggestion ids that weren't really done


@app.post("/api/todoist/suggestions", dependencies=[logged_in])
async def decide_suggestions(body: TodoistDecision):
    try:
        return await todoist.decide(body.day, body.accept, body.reject)
    except todoist.TodoistError as e:
        raise HTTPException(502, str(e))


class TodoistOptions(BaseModel):
    confirm: bool
    create: bool


@app.put("/api/config/todoist/options", dependencies=[logged_in])
def save_todoist_options(body: TodoistOptions):
    prefs.put("todoist_confirm", "1" if body.confirm else "0")
    prefs.put("todoist_create", "1" if body.create else "0")
    return _config_summary()


@app.get("/api/todoist/projects", dependencies=[logged_in])
async def todoist_projects():
    try:
        return await todoist.fetch_projects()
    except todoist.TodoistError as e:
        raise HTTPException(502, str(e))


class TodoistProjects(BaseModel):
    project_ids: list[str]


@app.put("/api/config/todoist/projects", dependencies=[logged_in])
async def save_todoist_projects(body: TodoistProjects):
    known = todoist.project_names()
    ids = [p for p in body.project_ids if p in known]
    prefs.put("todoist_projects", ",".join(ids))
    return _config_summary()


@app.delete("/api/config/todoist", dependencies=[logged_in])
def remove_todoist():
    prefs.put("todoist_token", "")
    prefs.put("todoist_projects", "")
    return _config_summary()


class GeneralConfig(BaseModel):
    timezone: str
    late_entry_until: str
    morning_reminder: str = ""  # empty = off
    evening_reminder: str = ""
    workdays: list[int] | None = None   # Monday = 0; None = leave unchanged
    weekly_summary: bool | None = None


@app.put("/api/config/general", dependencies=[logged_in])
def save_general(body: GeneralConfig):
    try:
        ZoneInfo(body.timezone)
    except Exception:
        raise HTTPException(400, "Unknown time zone.")
    for name in ("late_entry_until", "morning_reminder", "evening_reminder"):
        value = getattr(body, name).strip()
        if value and not TIME_RE.match(value):
            raise HTTPException(400, "Times must look like 09:00.")
    if not body.late_entry_until.strip():
        raise HTTPException(400, "Set until when yesterday can be logged.")
    prefs.put("timezone", body.timezone)
    prefs.put("late_entry_until", body.late_entry_until)
    prefs.put("morning_reminder", body.morning_reminder)
    prefs.put("evening_reminder", body.evening_reminder)
    if body.workdays is not None:
        prefs.put("workdays", ",".join(str(d) for d in sorted(set(body.workdays)) if 0 <= d <= 6))
    if body.weekly_summary is not None:
        prefs.put("weekly_summary", "1" if body.weekly_summary else "0")
    return _config_summary()


# ---------- backups ----------

MAX_UPLOAD = 50 * 1024 * 1024


def _backup_summary() -> dict:
    return {"config": backups.public_config(), "runs": backups.runs(),
            "kinds": backup_targets.KINDS, "keep": backups.KEEP}


@app.get("/api/backups", dependencies=[logged_in])
def backup_status():
    return _backup_summary()


class BackupConfig(BaseModel):
    schedule: str
    time: str
    weekday: int = 6
    password: str = ""            # empty = keep the saved one
    clear_password: bool = False
    locations: dict[str, dict] = {}


@app.put("/api/backups/config", dependencies=[unlocked])
def save_backup_config(body: BackupConfig):
    if body.schedule not in ("off", "daily", "weekly") or not TIME_RE.match(body.time) or not 0 <= body.weekday <= 6:
        raise HTTPException(400, "Check the schedule settings.")
    try:
        backups.save_config(body.model_dump())
    except (backups.BackupError, backup_targets.TargetError) as e:
        raise HTTPException(400, str(e))
    backups.secure_local_backups()   # a new backup password: encrypt older plain copies
    return _backup_summary()


@app.post("/api/backups/test/{kind}", dependencies=[unlocked])
async def test_backup_location(kind: str, body: dict):
    """Test a location with the values in the form (empty secrets fall back to the saved ones)."""
    if kind not in backup_targets.KINDS:
        raise HTTPException(404)
    saved = backups.get_config()["locations"][kind]
    cfg = {**saved, **{k: v for k, v in body.items() if not (k in backup_targets.SECRET_FIELDS and not v)}}
    try:
        await asyncio.to_thread(lambda: backup_targets.make(kind, cfg).test())
    except backup_targets.TargetError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/backups/run", dependencies=[logged_in])
async def backup_now():
    await backups.run_backup("manual")
    return _backup_summary()


@app.get("/api/backups/list/{kind}", dependencies=[logged_in])
async def list_backups(kind: str):
    try:
        return await backups.list_backups(kind)
    except (backups.BackupError, backup_targets.TargetError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/backups/download/{kind}/{name}", dependencies=[logged_in])
async def download_backup(kind: str, name: str):
    try:
        data = await backups.fetch(kind, name)
    except (backups.BackupError, backup_targets.TargetError) as e:
        raise HTTPException(400, str(e))
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def _restore_allowed(request: Request, setup_code: str) -> None:
    """Logged in, or a brand-new install with no password yet (restoring onto a fresh server),
    which also needs the setup code from the server logs."""
    if auth.is_logged_in(request):
        return
    if auth.password_configured():
        raise HTTPException(401, "Not logged in")
    await _check_setup_code(request, setup_code)


async def _read_body(request: Request, limit: int) -> bytes:
    """The request body, refusing (without reading it all) anything over the limit."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "Choose a DayScore backup file (up to 50 MB).")
        chunks.append(chunk)
    return b"".join(chunks)


class RestorePick(BaseModel):
    kind: str
    name: str
    password: str = ""   # backup password, if the file is encrypted


@app.post("/api/backups/restore/prepare", dependencies=[logged_in])
async def prepare_restore(body: RestorePick):
    try:
        data = await backups.fetch(body.kind, body.name)
        return await asyncio.to_thread(backups.stage, data, body.password)
    except (backups.BackupError, backup_targets.TargetError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/backups/restore/upload")
async def upload_restore(request: Request):
    # The backup password comes in a header, never in the URL (URLs end up in logs).
    await _restore_allowed(request, request.headers.get("x-setup-code", ""))
    data = await _read_body(request, MAX_UPLOAD)
    if not data:
        raise HTTPException(400, "Choose a DayScore backup file (up to 50 MB).")
    try:
        return await asyncio.to_thread(backups.stage, data, request.headers.get("x-backup-password", ""))
    except backups.BackupError as e:
        raise HTTPException(400, str(e))


class RestoreCommit(BaseModel):
    token: str
    account_password: str = ""   # current DayScore password (not needed on a fresh install)
    setup_code: str = ""         # fresh install only


@app.post("/api/backups/restore/commit")
async def commit_restore(body: RestoreCommit, request: Request):
    await _restore_allowed(request, body.setup_code)
    if auth.password_configured():
        await _confirm(request, body.account_password)
    try:
        result = await backups.commit(body.token)
    except backups.BackupError as e:
        raise HTTPException(400, str(e))
    telegram.restart()   # the restored settings may have a different bot
    resp = JSONResponse({**result, "relogin": True})
    resp.delete_cookie(auth.COOKIE_NAME)   # sign in again with the restored password
    return resp


# ---------- export ----------

@app.get("/api/export.{fmt}", dependencies=[logged_in])
def export(fmt: str):
    entries = db.list_entries()
    stamp = service.today().isoformat()
    if fmt == "json":
        body = json.dumps(entries, ensure_ascii=False, indent=2)
        media = "application/json"
    elif fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["day", "score", "title", "summary", "activities", "reason", "tip", "raw_text"])
        def cell(value):
            # A leading = + - @ would run as a formula in Excel; prefix it so it stays text.
            text = str(value)
            return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text

        for e in entries:
            writer.writerow([cell(v) for v in (
                e["day"], e["score"], e["title"], e["summary"],
                "; ".join(f"{a['text']} ({a['category']})" for a in e["activities"]),
                e["reason"], e["tip"], e["raw_text"])])
        body, media = buf.getvalue(), "text/csv"
    else:
        raise HTTPException(404)
    return Response(body, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="dayscore-{stamp}.{fmt}"'})


@app.get("/healthz", include_in_schema=False)
def health():
    try:
        db.get_setting("healthz")
    except Exception:
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True, "version": VERSION, "telegram": telegram.status["state"]}

"""Web server: JSON API + the dashboard. Also starts the Telegram bot in the background."""

import asyncio
import csv
import io
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, db, prefs, scoring, security, service, telegram, todoist
from .config import settings
from .scoring import CATEGORIES, ScoringError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# The HTTP client logs full request URLs, and Telegram puts the bot token in the URL.
for noisy in ("httpx2", "httpx", "httpcore", "httpcore2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("dayscore")

STATIC = Path(__file__).parent / "static"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if not auth.password_configured() and not settings.disable_auth:
        log.info("No password yet - open the web page to create one.")
    telegram.restart()
    sync_task = asyncio.create_task(todoist.sync_loop())
    yield
    telegram.stop()
    sync_task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
logged_in = Depends(auth.require_login)


def _versioned(html: str) -> str:
    """Add ?v=<file mtime> to asset URLs so browsers never run a stale app.js/style.css."""
    for name in ("app.js", "style.css"):
        version = int((STATIC / name).stat().st_mtime)
        html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
    return html


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
    await asyncio.sleep(1.5)  # slow down guessing
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


@app.post("/api/setup")
def first_setup(body: Password):
    """Create the password on first visit. Only works while no password exists."""
    if auth.password_configured():
        raise HTTPException(400, "A password is already set.")
    if len(body.password) < 8:
        raise HTTPException(400, "Use at least 8 characters.")
    auth.set_password(body.password)
    return _session_response({"ok": True})


@app.post("/api/login")
async def login(body: Password, request: Request):
    security.check_lock(request)
    if not auth.password_configured():
        raise HTTPException(400, "No password yet. Reload the page to create one.")
    if not auth.check_password(body.password):
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
    return resp


class PasswordChange(BaseModel):
    current: str
    new: str


@app.put("/api/password", dependencies=[logged_in])
async def change_password(body: PasswordChange, request: Request):
    security.check_lock(request)
    if not auth.check_password(body.current):
        await _fail(request, 400, "The current password is wrong.")
    if len(body.new) < 8:
        raise HTTPException(400, "Use at least 8 characters.")
    auth.set_password(body.new)
    return _session_response({"ok": True})  # re-issue this device's cookie; others are logged out


# ---------- security settings (every change needs the password) ----------

class Confirm(BaseModel):
    password: str


async def _confirm(request: Request, password: str) -> None:
    security.check_lock(request)
    if not auth.check_password(password):
        await _fail(request, 400, "The password is wrong.")


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
        },
        "telegram": {
            "token_set": bool(token),
            "token_hint": prefs.mask(token),
            "bot_username": prefs.get("telegram_bot_username"),
            "linked": prefs.telegram_user_id() is not None,
            "user_name": prefs.get("telegram_user_name"),
            "link_url": telegram.link_url(),
            "link_code": "" if prefs.telegram_user_id() is not None else prefs.get("telegram_link_code"),
        },
        "todoist": {
            "connected": bool(todoist.token()),
            "token_hint": prefs.mask(todoist.token()),
            "project_ids": todoist.project_ids(),
        },
        "general": {
            "timezone": prefs.get("timezone"),
            "late_entry_until": prefs.late_entry_until().strftime("%H:%M"),
            "morning_reminder": prefs.get("morning_reminder"),
            "evening_reminder": prefs.get("evening_reminder"),
        },
        "password_from_env": bool(settings.app_password) and not db.get_setting(auth.HASH_KEY),
        "security": security.summary(),
    }


@app.get("/api/state", dependencies=[logged_in])
def state():
    """Everything the dashboard needs in one call."""
    return {
        "app_name": settings.app_name,
        "today": service.today().isoformat(),
        "loggable_days": [d.isoformat() for d in service.loggable_days()],
        "default_day": service.default_day().isoformat(),
        "late_entry_until": prefs.late_entry_until().strftime("%H:%M"),
        "categories": CATEGORIES,
        "entries": db.list_entries(),
        "todoist_done": db.todoist_by_day(),
        "stats": service.stats(),
        "priorities": db.get_setting("priorities", ""),
        "config": _config_summary(),
    }


class NewNote(BaseModel):
    day: date
    text: str


@app.post("/api/entries", dependencies=[logged_in])
async def add_note(body: NewNote):
    try:
        entry, previous = await service.log_day(body.day, body.text, source="web")
    except service.NotAllowed as e:
        raise HTTPException(400, str(e))
    except ScoringError as e:
        raise HTTPException(502, str(e))
    return {"entry": entry, "previous_score": previous}


class Priorities(BaseModel):
    priorities: str


@app.put("/api/settings", dependencies=[logged_in])
def save_priorities(body: Priorities):
    db.set_setting("priorities", body.priorities.strip()[:4000])
    return {"ok": True}


# ---------- configuration from the Settings page ----------

class AIConfig(BaseModel):
    provider: str       # anthropic | openrouter
    api_key: str = ""   # empty = keep the saved key
    model: str


@app.put("/api/config/ai", dependencies=[logged_in])
async def save_ai(body: AIConfig):
    if body.provider not in prefs.AI_KEYS:
        raise HTTPException(400, "Unknown provider.")
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


@app.put("/api/config/telegram", dependencies=[logged_in])
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


@app.post("/api/config/telegram/unlink", dependencies=[logged_in])
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


@app.put("/api/config/todoist", dependencies=[logged_in])
async def save_todoist(body: TodoistConfig):
    tok = body.token.strip()
    try:
        await todoist.fetch_projects(tok)  # validates the token
    except todoist.TodoistError as e:
        raise HTTPException(400, str(e))
    prefs.put("todoist_token", tok)
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
    db.set_setting("todoist.sync_days", "30")  # backfill the last month for newly picked projects
    try:
        await todoist.sync()
    except todoist.TodoistError as e:
        log.warning("Todoist sync after saving projects failed: %s", e)
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
    return _config_summary()


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
        for e in entries:
            writer.writerow([e["day"], e["score"], e["title"], e["summary"],
                             "; ".join(f"{a['text']} ({a['category']})" for a in e["activities"]),
                             e["reason"], e["tip"], e["raw_text"]])
        body, media = buf.getvalue(), "text/csv"
    else:
        raise HTTPException(404)
    return Response(body, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="dayscore-{stamp}.{fmt}"'})


@app.get("/healthz", include_in_schema=False)
def health():
    return {"ok": True}

"""Telegram bot (long polling, so no public URL or webhook is needed) plus daily reminders.

The bot is configured from Settings: paste a token, then open the "connect" link, which
sends `/start <one-time code>` and links your Telegram account. After that the bot only
answers you.
"""

import asyncio
import html
import logging
import os
import secrets
from datetime import date, timedelta

import httpx2 as httpx

from . import db, prefs, service
from .scoring import ScoringError

log = logging.getLogger("dayscore.telegram")

# Overridable for self-hosted Bot API servers (and tests).
API = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/") + "/bot{token}/{method}"

# Notes waiting for a "yesterday or today?" answer, keyed by a short random token.
_pending: dict[str, str] = {}
_task: asyncio.Task | None = None


class Bot:
    def __init__(self, token: str):
        self.token = token
        self.client = httpx.AsyncClient(timeout=70.0)

    async def call(self, method: str, **params) -> dict:
        resp = await self.client.post(API.format(token=self.token, method=method), json=params)
        data = resp.json()
        if not data.get("ok"):
            log.warning("Telegram %s failed: %s", method, data.get("description"))
        return data

    async def send(self, chat_id: int, text: str, **extra) -> None:
        await self.call(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
            link_preview_options={"is_disabled": True}, **extra,
        )


# ---------- setup helpers used by the Settings page ----------

async def check_token(token: str) -> dict:
    """Return the bot's info, or raise ValueError with a readable message."""
    if ":" not in token:
        raise ValueError("That doesn't look like a bot token. It should look like 123456789:AA…")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(API.format(token=token, method="getMe"))
            data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ValueError("Couldn't reach Telegram. Check the server's internet connection.") from e
    if not data.get("ok"):
        raise ValueError("Telegram didn't accept this token. Copy it again from @BotFather.")
    return data["result"]


MAX_LINK_ATTEMPTS = 5
_failed_links = 0


def new_link_code() -> str:
    """A 6-digit code: sent automatically by the deep link, or typed to the bot by hand.
    It's replaced after a few wrong guesses, so it can't be brute-forced."""
    global _failed_links
    _failed_links = 0
    code = f"{secrets.randbelow(10**6):06d}"
    prefs.put("telegram_link_code", code)
    return code


def link_url() -> str:
    username, code = prefs.get("telegram_bot_username"), prefs.get("telegram_link_code")
    return f"https://t.me/{username}?start={code}" if username and code else ""


def restart() -> None:
    """(Re)start the bot with the current token; stop it if there is none."""
    global _task
    if _task:
        _task.cancel()
        _task = None
    token = prefs.get("telegram_token")
    if token:
        _task = asyncio.create_task(run(token))


def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None


# ---------- message formatting ----------

def _esc(value: str) -> str:
    return html.escape(value, quote=False)


def _meter(score: int) -> str:
    filled = round(score / 10)
    return "▰" * filled + "▱" * (10 - filled)


def _day_label(day: date) -> str:
    t = service.today()
    if day == t:
        return "today"
    if day == t - timedelta(days=1):
        return "yesterday"
    return f"{day:%a %d %b}"


def format_entry(entry: dict, previous: int | None = None) -> str:
    day = date.fromisoformat(entry["day"])
    head = f"<b>{entry['score']}</b>/100  {_meter(entry['score'])}"
    if previous is not None and previous != entry["score"]:
        head += f"  <i>(was {previous})</i>"
    lines = [
        f"📅 <b>{_esc(day.strftime('%A, %d %B'))}</b> · {_day_label(day)}",
        head,
        f"<b>{_esc(entry['title'])}</b>",
        "",
        *[f"• {_esc(a['text'])}" for a in entry["activities"]],
    ]
    return "\n".join(lines)


def format_stats() -> str:
    s = service.stats()
    if not s["total_days"]:
        return "No days logged yet. Send me what you did today!"

    def fmt(v):
        return "–" if v is None else f"{v:g}"

    lines = [
        "📊 <b>Your stats</b>",
        f"Last 7 days: <b>{fmt(s['avg_7'])}</b> (previous 7: {fmt(s['avg_7_prev'])})",
        f"Last 30 days: <b>{fmt(s['avg_30'])}</b>",
        f"All time: <b>{fmt(s['avg_all'])}</b> over {s['total_days']} days",
        f"🔥 Streak: <b>{s['streak']}</b> day{'s' if s['streak'] != 1 else ''}",
    ]
    if s["best"]:
        best_day = date.fromisoformat(s["best"]["day"])
        lines.append(f"🏆 Best: <b>{s['best']['score']}</b> on {best_day:%d %b %Y} – {_esc(s['best']['title'])}")
    return "\n".join(lines)


def format_week() -> str:
    t = service.today()
    lines = ["🗓 <b>Last 7 days</b>"]
    scores = []
    for i in range(6, -1, -1):
        d = t - timedelta(days=i)
        e = db.get_entry(d)
        if e:
            scores.append(e["score"])
            lines.append(f"<code>{d:%a %d}</code>  <b>{e['score']:>3}</b>  {_esc(e['title'])}")
        else:
            lines.append(f"<code>{d:%a %d}</code>    –  <i>not logged</i>")
    if scores:
        lines.append(f"\nAverage: <b>{sum(scores) / len(scores):.1f}</b>")
    return "\n".join(lines)


HELP = (
    "Just send me a message about what you did today – in Slovenian or English, "
    "as short or long as you like. I'll score it from 0 to 100.\n\n"
    "Send more messages the same day to add things; the day gets re-scored.\n"
    "Until {cutoff} you can still log yesterday.\n\n"
    "/today – today's score\n"
    "/yesterday – yesterday's score\n"
    "/week – last 7 days\n"
    "/stats – averages, streak, best day"
)


def _help() -> str:
    return HELP.format(cutoff=prefs.late_entry_until().strftime("%H:%M"))


# ---------- handlers ----------

async def _log_and_reply(bot: Bot, chat_id: int, day: date, text: str) -> None:
    await bot.call("sendChatAction", chat_id=chat_id, action="typing")
    try:
        entry, previous = await service.log_day(day, text, source="telegram")
    except (ScoringError, service.NotAllowed) as e:
        await bot.send(chat_id, f"⚠️ {_esc(str(e))}")
        return
    except Exception:
        log.exception("Failed to log day")
        await bot.send(chat_id, "⚠️ Something went wrong while saving. Check the server logs.")
        return
    if previous is not None:
        await bot.send(chat_id, f"➕ Added to {_day_label(day)}.")
    await bot.send(chat_id, format_entry(entry, previous))


async def _handle_message(bot: Bot, msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
    t = service.today()

    if command in ("/start", "/help"):
        await bot.send(chat_id, _help())
    elif command in ("/today", "/yesterday"):
        day = t if command == "/today" else t - timedelta(days=1)
        entry = db.get_entry(day)
        await bot.send(chat_id, format_entry(entry) if entry else f"Nothing logged for {_day_label(day)} yet.")
    elif command == "/week":
        await bot.send(chat_id, format_week())
    elif command == "/stats":
        await bot.send(chat_id, format_stats())
    elif command:
        await bot.send(chat_id, "Unknown command. Try /help")
    elif not text:
        await bot.send(chat_id, "I can only read text messages for now.")
    elif service.needs_day_choice():
        token = secrets.token_hex(4)
        _pending[token] = text
        yesterday = t - timedelta(days=1)
        await bot.send(
            chat_id,
            "Is this for yesterday or today?",
            reply_markup={"inline_keyboard": [[
                {"text": f"Yesterday ({yesterday:%a %d})", "callback_data": f"day:{token}:{yesterday.isoformat()}"},
                {"text": f"Today ({t:%a %d})", "callback_data": f"day:{token}:{t.isoformat()}"},
            ]]},
        )
    else:
        await _log_and_reply(bot, chat_id, t, text)


async def _handle_callback(bot: Bot, cb: dict) -> None:
    await bot.call("answerCallbackQuery", callback_query_id=cb["id"])
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    kind, _, rest = (cb.get("data") or "").partition(":")
    token, _, day_str = rest.partition(":")
    if kind != "day" or chat_id is None:
        return
    text = _pending.pop(token, None)
    day = date.fromisoformat(day_str)
    await bot.call(
        "editMessageText", chat_id=chat_id, message_id=msg["message_id"],
        text=f"Logging for {_day_label(day)}…" if text else "This choice expired, please send the note again.",
    )
    if text:
        await _log_and_reply(bot, chat_id, day, text)


async def _try_link(bot: Bot, msg: dict) -> None:
    """Before an account is linked, only the correct code does anything. It arrives either
    as `/start <code>` (from the deep link) or as a plain message (typed by hand)."""
    global _failed_links
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    text = (msg.get("text") or "").strip()
    parts = text.split()
    if parts and parts[0].split("@")[0] == "/start":
        attempt = parts[1] if len(parts) > 1 else ""
    else:
        attempt = text.replace(" ", "")
    code = prefs.get("telegram_link_code")

    if code and attempt and secrets.compare_digest(attempt, code):
        prefs.put("telegram_user_id", str(user["id"]))
        prefs.put("telegram_user_name", user.get("username") or user.get("first_name") or "")
        prefs.put("telegram_link_code", "")
        log.info("Linked Telegram user %s", user["id"])
        await bot.send(chat_id, "✅ <b>Connected!</b> From now on I only listen to you.\n\n" + _help())
        return

    if attempt:
        _failed_links += 1
        log.info("Wrong link code from Telegram user %s (attempt %d)", user.get("id"), _failed_links)
        if _failed_links >= MAX_LINK_ATTEMPTS:
            new_link_code()
            log.info("Too many wrong link codes - generated a new one")
    else:
        log.info("Link attempt without a code from Telegram user %s", user.get("id"))
    await bot.send(chat_id, "👋 To connect, send me the <b>6-digit code</b> shown in "
                            "DayScore → Settings → Telegram.")


async def poll(bot: Bot) -> None:
    offset = 0
    while True:
        try:
            data = await bot.call("getUpdates", offset=offset, timeout=50,
                                  allowed_updates=["message", "callback_query"])
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Telegram polling error: %s", e)
            await asyncio.sleep(5)
            continue
        if data.get("error_code") == 401:
            log.error("Telegram rejected the bot token; stopping the bot.")
            return
        for update in data.get("result", []):
            offset = update["update_id"] + 1
            source = update.get("message") or update.get("callback_query") or {}
            user = source.get("from", {})
            owner = prefs.telegram_user_id()
            try:
                if owner is None:
                    if "message" in update:
                        await _try_link(bot, update["message"])
                elif user.get("id") != owner:
                    log.info("Ignored update from user %s (@%s)", user.get("id"), user.get("username"))
                elif "message" in update:
                    await _handle_message(bot, update["message"])
                elif "callback_query" in update:
                    await _handle_callback(bot, update["callback_query"])
            except Exception:
                log.exception("Error handling update")


async def reminders(bot: Bot) -> None:
    while True:
        await asyncio.sleep(60)
        chat_id = prefs.telegram_user_id()
        if chat_id is None:
            continue
        try:
            moment = service.now()
            t = moment.date()
            yesterday = t - timedelta(days=1)
            m, e = prefs.morning_reminder(), prefs.evening_reminder()
            has_history = bool(db.recent_entries(before=t + timedelta(days=1), limit=1))

            if (m and has_history and moment.time() >= m and service.in_late_window(moment)
                    and db.get_entry(yesterday) is None and db.mark_reminder(yesterday, "morning")):
                cutoff = prefs.late_entry_until().strftime("%H:%M")
                await bot.send(chat_id, f"☀️ Good morning! You didn't log yesterday ({yesterday:%A}). "
                                        f"You have until {cutoff} – just reply with what you did.")

            if (e and moment.time() >= e and db.get_entry(t) is None
                    and db.mark_reminder(t, "evening")):
                await bot.send(chat_id, "🌙 How did today go? Send me a few words about what you got done.")
        except Exception:
            log.exception("Reminder check failed")


async def run(token: str) -> None:
    bot = Bot(token)
    try:
        me = await bot.call("getMe")
        if not me.get("ok"):
            log.error("Telegram token rejected; bot not started.")
            return
        prefs.put("telegram_bot_username", me["result"]["username"])
        code = prefs.get("telegram_link_code")
        if prefs.telegram_user_id() is None and not (len(code) == 6 and code.isdigit()):
            new_link_code()
        log.info("Telegram bot @%s is running", me["result"]["username"])
        await bot.call("setMyCommands", commands=[
            {"command": "today", "description": "Today's score"},
            {"command": "yesterday", "description": "Yesterday's score"},
            {"command": "week", "description": "Last 7 days"},
            {"command": "stats", "description": "Averages, streak, best day"},
            {"command": "help", "description": "How to use this bot"},
        ])
        await asyncio.gather(poll(bot), reminders(bot))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Telegram bot crashed")
    finally:
        await bot.client.aclose()

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
import time
from datetime import date, timedelta
from datetime import time as time_of_day

import httpx2 as httpx

from . import db, prefs, service, todoist
from .scoring import ScoringError

log = logging.getLogger("dayscore.telegram")

# Overridable for self-hosted Bot API servers (and tests).
API = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/") + "/bot{token}/{method}"

# Notes waiting for a "yesterday or today?" answer (or a retry), keyed by a short random token.
_pending: dict[str, str] = {}
_task: asyncio.Task | None = None

# Shown in Settings: starting | running | retrying | stopped | error
status = {"state": "stopped", "detail": "", "since": 0.0}


def _set_status(state: str, detail: str = "") -> None:
    if status["state"] != state or status["detail"] != detail:
        status.update(state=state, detail=detail, since=time.time())


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


MAX_LINK_ATTEMPTS = 5      # wrong guesses before the code is replaced
MAX_LINK_ROUNDS = 3        # replaced codes before linking pauses
LINK_PAUSE_SECONDS = 3600
_failed_links = 0
_link_rounds = 0
_link_paused_until = 0.0


def new_link_code(by_owner: bool = True) -> str:
    """A 6-digit code: sent automatically by the deep link, or typed to the bot by hand.
    It's replaced after a few wrong guesses, and linking pauses for an hour after several
    replaced codes, so it can't be brute-forced. A new code made in Settings lifts the pause."""
    global _failed_links, _link_rounds, _link_paused_until
    _failed_links = 0
    if by_owner:
        _link_rounds = 0
        _link_paused_until = 0.0
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
    else:
        _set_status("stopped")


def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
    _set_status("stopped")


# ---------- message formatting ----------

def _esc(value: str) -> str:
    return html.escape(value, quote=False)


escape = _esc


async def notify(text: str) -> bool:
    """Send the owner a message (backup alerts...). Quietly does nothing without a linked bot."""
    token, chat_id = prefs.get("telegram_token"), prefs.telegram_user_id()
    if not token or chat_id is None:
        return False
    bot = Bot(token)
    try:
        data = await bot.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                              link_preview_options={"is_disabled": True})
        return bool(data.get("ok"))
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Couldn't send a Telegram notification: %s", e)
        return False
    finally:
        await bot.client.aclose()


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
    if entry.get("reason"):
        lines += ["", f"💬 <i>{_esc(entry['reason'])}</i>"]
    if entry.get("tip"):
        lines.append(f"💡 {_esc(entry['tip'])}")
    return "\n".join(lines)


MOODS = ["😞", "😕", "😐", "🙂", "😄"]


def _mood_keyboard(day: date) -> dict:
    return {"inline_keyboard": [[{"text": m, "callback_data": f"mood:{day.isoformat()}:{i + 1}"}
                                 for i, m in enumerate(MOODS)]]}


def format_weekly() -> str | None:
    """Sunday recap: this week vs last week, best day, where the effort went."""
    t = service.today()
    this_week = [e for i in range(7) if (e := db.get_entry(t - timedelta(days=i)))]
    if not this_week:
        return None
    last_week = [e for i in range(7, 14) if (e := db.get_entry(t - timedelta(days=i)))]
    avg = sum(e["score"] for e in this_week) / len(this_week)
    lines = ["🗓 <b>Your week</b>",
             f"Average <b>{avg:.0f}</b> over {len(this_week)} logged day{'s' if len(this_week) != 1 else ''}"]
    if last_week:
        prev = sum(e["score"] for e in last_week) / len(last_week)
        diff = avg - prev
        arrow = "▲" if diff > 0.5 else "▼" if diff < -0.5 else "■"
        lines[-1] += f"  {arrow} {abs(diff):.0f} vs last week"
    best = max(this_week, key=lambda e: e["score"])
    lines.append(f"🏆 Best: <b>{best['score']}</b> on {date.fromisoformat(best['day']):%A} – {_esc(best['title'])}")
    counts: dict[str, int] = {}
    for e in this_week:
        for a in e["activities"]:
            counts[a["category"]] = counts.get(a["category"], 0) + 1
    if counts:
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
        lines.append("Most time on: " + ", ".join(f"{c} ({n})" for c, n in top))
    moods = [e["mood"] for e in this_week if e.get("mood")]
    if moods:
        lines.append(f"Mood: {MOODS[round(sum(moods) / len(moods)) - 1]}")
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
        entry, previous, info = await service.log_day(day, text, source="telegram")
    except ScoringError as e:
        # Keep the note for a moment so one tap can try again (e.g. the AI provider was down).
        token = secrets.token_hex(4)
        _pending[token] = text
        await bot.send(chat_id, f"⚠️ {_esc(str(e))}\nYour note wasn't saved yet.",
                       reply_markup={"inline_keyboard": [[
                           {"text": "Try again", "callback_data": f"retry:{token}:{day.isoformat()}"}]]})
        return
    except service.NotAllowed as e:
        await bot.send(chat_id, f"⚠️ {_esc(str(e))}")
        return
    except Exception:
        log.exception("Failed to log day")
        await bot.send(chat_id, "⚠️ Something went wrong while saving. Check the server logs.")
        return
    if previous is not None:
        await bot.send(chat_id, f"➕ Added to {_day_label(day)}.")
    await bot.send(chat_id, format_entry(entry, previous),
                   **({} if entry.get("mood") else {"reply_markup": _mood_keyboard(day)}))

    if info["created"]:
        lines = [f"• {_esc(c['content'])}" + (f" <i>(due {date.fromisoformat(c['due']):%a %d %b})</i>" if c["due"] else "")
                 for c in info["created"]]
        await bot.send(chat_id, "🗒 <b>Added to your Todoist Inbox:</b>\n" + "\n".join(lines))
    if info["closed"]:
        lines = [f"• {_esc(t['content'])}" + ("" if t["ok"] else " ⚠️ <i>Todoist didn't accept it</i>") for t in info["closed"]]
        await bot.send(chat_id, "✅ <b>Ticked off in Todoist:</b>\n" + "\n".join(lines))
    keyboard = _suggestion_keyboard(day)
    if keyboard:
        await bot.send(chat_id, "📋 <b>Your note seems to finish these Todoist tasks.</b>\n"
                                "Tap any that aren't done to deselect them, then confirm.", reply_markup=keyboard)


def _suggestion_keyboard(day: date) -> dict | None:
    pending = db.suggestions(day, status="pending")
    if not pending:
        return None
    rows = [[{"text": f"{'✅' if s['selected'] else '⬜'} {s['content'][:45]}",
              "callback_data": f"sg:t:{s['suggestion_id']}"}] for s in pending]
    rows.append([{"text": "Tick off selected", "callback_data": f"sg:ok:{day.isoformat()}"},
                 {"text": "None of these", "callback_data": f"sg:no:{day.isoformat()}"}])
    return {"inline_keyboard": rows}


async def _handle_suggestion(bot: Bot, chat_id: int, message_id: int, action: str, value: str) -> None:
    """Toggle a suggested task, or confirm the selection (sg:t:<id>, sg:ok:<day>, sg:no:<day>)."""
    if action == "t":
        match = next((s for s in db.suggestions(status="pending") if str(s["suggestion_id"]) == value), None)
        if not match:
            return
        db.set_suggestion(match["suggestion_id"], selected=not match["selected"])
        day = date.fromisoformat(match["day"])
        await bot.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                       reply_markup=_suggestion_keyboard(day) or {"inline_keyboard": []})
        return

    day = date.fromisoformat(value)
    pending = db.suggestions(day, status="pending")
    if not pending:
        await bot.call("editMessageText", chat_id=chat_id, message_id=message_id,
                       text="These were already handled (maybe on the website).")
        return
    accept = [s["suggestion_id"] for s in pending if action == "ok" and s["selected"]]
    reject = [s["suggestion_id"] for s in pending if s["suggestion_id"] not in accept]
    result = await todoist.decide(day, accept, reject)
    lines = []
    if result["closed"]:
        lines.append("✅ Ticked off in Todoist:\n" + "\n".join(f"• {c}" for c in result["closed"]))
    if result["failed"]:
        lines.append("⚠️ Todoist didn't accept these, please tick them off yourself:\n"
                     + "\n".join(f"• {c}" for c in result["failed"]))
    text = "\n\n".join(lines) or "Okay, nothing was ticked off."
    await bot.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text)


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
    if kind == "sg" and chat_id is not None:
        await _handle_suggestion(bot, chat_id, msg["message_id"], token, day_str)
        return
    if kind == "mood" and chat_id is not None:
        # mood:<day>:<1-5> - token holds the day here, day_str the mood.
        try:
            service.set_mood(date.fromisoformat(token), int(day_str))
        except (ValueError, service.NotAllowed):
            return
        await bot.call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg["message_id"],
                       reply_markup={"inline_keyboard": [[{"text": f"Mood: {MOODS[int(day_str) - 1]}", "callback_data": "noop"}]]})
        return
    if kind not in ("day", "retry") or chat_id is None:
        return
    text = _pending.pop(token, None)
    day = date.fromisoformat(day_str)
    await bot.call(
        "editMessageText", chat_id=chat_id, message_id=msg["message_id"],
        text=(f"Trying again for {_day_label(day)}…" if kind == "retry" else f"Logging for {_day_label(day)}…")
        if text else "This expired, please send the note again.",
    )
    if text:
        await _log_and_reply(bot, chat_id, day, text)


async def _try_link(bot: Bot, msg: dict) -> None:
    """Before an account is linked, only the correct code does anything. It arrives either
    as `/start <code>` (from the deep link) or as a plain message (typed by hand)."""
    global _failed_links, _link_rounds, _link_paused_until
    chat_id = msg["chat"]["id"]
    if time.time() < _link_paused_until:
        await bot.send(chat_id, "Linking is paused for a while after too many wrong codes.")
        return
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
            new_link_code(by_owner=False)
            _link_rounds += 1
            log.info("Too many wrong link codes - generated a new one")
            if _link_rounds >= MAX_LINK_ROUNDS:
                _link_paused_until = time.time() + LINK_PAUSE_SECONDS
                log.warning("Telegram linking paused for an hour after repeated wrong codes")
    else:
        log.info("Link attempt without a code from Telegram user %s", user.get("id"))
    await bot.send(chat_id, "👋 To connect, send me the <b>6-digit code</b> shown in "
                            "DayScore → Settings → Telegram.")


def _backoff(failures: int, data: dict | None = None) -> float:
    retry_after = ((data or {}).get("parameters") or {}).get("retry_after")
    return float(retry_after) if retry_after else min(60.0, 2.0 ** min(failures, 6))


async def poll(bot: Bot) -> None:
    offset = 0
    failures = 0
    while True:
        try:
            data = await bot.call("getUpdates", offset=offset, timeout=50,
                                  allowed_updates=["message", "callback_query"])
        except (httpx.HTTPError, ValueError) as e:
            failures += 1
            _set_status("retrying", "Can't reach Telegram right now.")
            log.warning("Telegram polling error: %s", e)
            await asyncio.sleep(_backoff(failures))
            continue
        if data.get("error_code") == 401:
            _set_status("error", "Telegram rejected the bot token. Connect the bot again.")
            log.error("Telegram rejected the bot token; stopping the bot.")
            return
        if not data.get("ok"):
            # 409: another copy of DayScore uses this bot token; 429: slow down; 5xx: Telegram trouble.
            failures += 1
            detail = ("Another program is using this bot token." if data.get("error_code") == 409
                      else data.get("description") or "Telegram returned an error.")
            _set_status("retrying", detail)
            await asyncio.sleep(_backoff(failures, data))
            continue
        failures = 0
        _set_status("running")
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

            weekly_at = e or time_of_day(20, 0)
            if (prefs.get("weekly_summary") == "1" and t.weekday() == 6 and moment.time() >= weekly_at
                    and (summary := format_weekly()) and db.mark_reminder(t, "weekly")):
                await bot.send(chat_id, summary)
        except Exception:
            log.exception("Reminder check failed")


async def run(token: str) -> None:
    bot = Bot(token)
    _set_status("starting")
    try:
        failures = 0
        while True:   # the network may not be up yet right after a reboot: keep trying
            try:
                me = await bot.call("getMe")
            except (httpx.HTTPError, ValueError) as e:
                failures += 1
                _set_status("retrying", "Can't reach Telegram right now.")
                log.warning("Telegram not reachable yet (%s); retrying", e)
                await asyncio.sleep(_backoff(failures))
                continue
            if me.get("ok"):
                break
            if me.get("error_code") in (401, 404):
                _set_status("error", "Telegram rejected the bot token. Connect the bot again.")
                log.error("Telegram token rejected; bot not started.")
                return
            failures += 1
            await asyncio.sleep(_backoff(failures, me))
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
        _set_status("running")
        await asyncio.gather(poll(bot), reminders(bot))
    except asyncio.CancelledError:
        _set_status("stopped")
        raise
    except Exception:
        _set_status("error", "The bot stopped because of an error. See the server logs.")
        log.exception("Telegram bot crashed")
    finally:
        await bot.client.aclose()

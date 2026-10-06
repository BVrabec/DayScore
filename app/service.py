"""Core logic shared by the website and the Telegram bot."""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta

from . import db, prefs, scoring, todoist

log = logging.getLogger("dayscore")

MAX_NOTE = 4000             # characters in one note
MAX_DAY = 20000             # characters in a whole day (notes are appended)
MAX_AI_CALLS_PER_DAY = 40   # protects the API key from a runaway client
AI_CALLS_KEY = "ai.calls"

# One lock per day: a web note and a Telegram note arriving together must not overwrite each
# other while the AI is scoring (seconds).
_day_locks: dict[date, asyncio.Lock] = {}


class NotAllowed(Exception):
    pass


def _lock_for(day: date) -> asyncio.Lock:
    if len(_day_locks) > 50:   # forget old days
        for d in [d for d, lock in _day_locks.items() if not lock.locked()]:
            del _day_locks[d]
    return _day_locks.setdefault(day, asyncio.Lock())


def _count_ai_call() -> None:
    today_key = today().isoformat()
    state = json.loads(db.get_setting(AI_CALLS_KEY, "{}"))
    count = state.get("n", 0) if state.get("day") == today_key else 0
    if count >= MAX_AI_CALLS_PER_DAY:
        raise NotAllowed(f"That's {MAX_AI_CALLS_PER_DAY} scorings today, the daily limit that protects your "
                         "API key. It resets at midnight.")
    db.set_setting(AI_CALLS_KEY, json.dumps({"day": today_key, "n": count + 1}))


def _check_mood(mood: int | None) -> int | None:
    if mood is not None and not 1 <= mood <= 5:
        raise NotAllowed("Mood goes from 1 to 5.")
    return mood


def now() -> datetime:
    return datetime.now(prefs.tz())


def today() -> date:
    return now().date()


def in_late_window(moment: datetime | None = None) -> bool:
    """True while the previous day can still be logged (e.g. before 12:00)."""
    moment = moment or now()
    return moment.time() < prefs.late_entry_until()


def loggable_days() -> list[date]:
    t = today()
    return [t, t - timedelta(days=1)] if in_late_window() else [t]


def needs_day_choice() -> bool:
    """Before the cutoff with yesterday still empty, we don't know which day a note is for."""
    return in_late_window() and db.get_entry(today() - timedelta(days=1)) is None


def default_day() -> date:
    return today() - timedelta(days=1) if needs_day_choice() else today()


async def log_day(day: date, text: str, source: str, mood: int | None = None) -> tuple[dict, int | None, dict]:
    """Score and store a note. A second note for the same day is appended and the day re-scored.

    Returns (entry, previous score or None, todoist info). The info has "suggested" (tasks
    waiting for the owner's OK), "closed" (ticked off right away) and "created" (new tasks).
    """
    text = text.strip()
    if not text:
        raise NotAllowed("The note is empty.")
    if len(text) > MAX_NOTE:
        raise NotAllowed(f"That note is very long. Keep it under {MAX_NOTE} characters.")
    if day not in loggable_days():
        cutoff = prefs.late_entry_until().strftime("%H:%M")
        raise NotAllowed(f"You can only log today, or yesterday before {cutoff}.")
    _check_mood(mood)

    async with _lock_for(day):
        existing = db.get_entry(day)
        full_text = f"{existing['raw_text']}\n\n{text}" if existing else text
        if len(full_text) > MAX_DAY:
            raise NotAllowed("This day already has a lot of text. Edit the day instead of adding more.")
        _count_ai_call()

        # Open Todoist tasks and already-created ones go into the prompt; a Todoist outage never
        # blocks scoring.
        tasks: list[dict] = []
        if todoist.enabled():
            try:
                tasks = await todoist.open_tasks(day)
            except todoist.TodoistError as e:
                log.warning("Scoring without Todoist: %s", e)
        blocks = [todoist.prompt_block(day, tasks) if tasks else "",
                  todoist.created_block(day) if todoist.can_create() else ""]
        context = "\n\n".join(b for b in blocks if b)

        result = await scoring.score_day(day, full_text, context, existing["score"] if existing else None)
        entry = db.save_entry(day, full_text, result, scoring.current_model(), source,
                              rubric=scoring.RUBRIC_VERSION, mood=mood)

    info = {"suggested": [], "closed": [], "created": []}
    found = todoist.matched(result.get("completed_task_ids", []), tasks)
    if found and todoist.confirm_first():
        info["suggested"] = todoist.suggest(day, found)
    elif found:
        info["closed"] = await todoist.close_matched(day, [t["id"] for t in found], tasks)
    if todoist.can_create() and result.get("new_todos"):
        info["created"] = await todoist.create_todos(day, result["new_todos"][:scoring.MAX_NEW_TODOS])
    return entry, existing["score"] if existing else None, info


async def edit_day(day: date, text: str) -> tuple[dict, int]:
    """Replace a day's whole note and score it again. Todoist isn't touched."""
    text = text.strip()
    if not text:
        raise NotAllowed("The note is empty. To remove the day, delete it instead.")
    if len(text) > MAX_DAY:
        raise NotAllowed(f"Keep the day under {MAX_DAY} characters.")
    if day > today():
        raise NotAllowed("That day hasn't happened yet.")
    async with _lock_for(day):
        existing = db.get_entry(day)
        if not existing:
            raise NotAllowed("Nothing is logged for that day.")
        _count_ai_call()
        result = await scoring.score_day(day, text)
        entry = db.save_entry(day, text, result, scoring.current_model(), existing["source"],
                              rubric=scoring.RUBRIC_VERSION)
    return entry, existing["score"]


async def delete_day(day: date) -> None:
    async with _lock_for(day):
        if not db.delete_entry(day):
            raise NotAllowed("Nothing is logged for that day.")
    log.info("Deleted the entry for %s", day)


def set_mood(day: date, mood: int | None) -> dict:
    _check_mood(mood)
    entry = db.get_entry(day)
    if not entry:
        raise NotAllowed("Log the day first, then add your mood.")
    return db.set_mood(day, mood)


def _avg(entries: list[dict]) -> float | None:
    return round(sum(e["score"] for e in entries) / len(entries), 1) if entries else None


def stats() -> dict:
    entries = db.list_entries()
    by_day = {e["day"]: e for e in entries}
    t = today()

    def window(start_offset: int, days: int) -> list[dict]:
        return [
            by_day[d.isoformat()]
            for i in range(start_offset, start_offset + days)
            if (d := t - timedelta(days=i)).isoformat() in by_day
        ]

    # Streak counts back from today, or from yesterday if today isn't logged yet.
    streak = 0
    cursor = t if t.isoformat() in by_day else t - timedelta(days=1)
    while cursor.isoformat() in by_day:
        streak += 1
        cursor -= timedelta(days=1)

    best = max(entries, key=lambda e: (e["score"], e["day"]), default=None)
    return {
        "total_days": len(entries),
        "avg_all": _avg(entries),
        "avg_7": _avg(window(0, 7)),
        "avg_7_prev": _avg(window(7, 7)),
        "avg_30": _avg(window(0, 30)),
        "streak": streak,
        "best": {"day": best["day"], "score": best["score"], "title": best["title"]} if best else None,
    }

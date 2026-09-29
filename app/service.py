"""Core logic shared by the website and the Telegram bot."""

import logging
from datetime import date, datetime, timedelta

from . import db, prefs, scoring, todoist

log = logging.getLogger("dayscore")


class NotAllowed(Exception):
    pass


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


async def log_day(day: date, text: str, source: str) -> tuple[dict, int | None]:
    """Score and store a note. A second note for the same day is appended and the day re-scored.

    Returns (entry, previous_score or None).
    """
    text = text.strip()
    if not text:
        raise NotAllowed("The note is empty.")
    if day not in loggable_days():
        cutoff = prefs.late_entry_until().strftime("%H:%M")
        raise NotAllowed(f"You can only log today, or yesterday before {cutoff}.")

    existing = db.get_entry(day)
    full_text = f"{existing['raw_text']}\n\n{text}" if existing else text

    # Open Todoist tasks go into the prompt; a Todoist outage never blocks scoring.
    tasks: list[dict] = []
    if todoist.enabled():
        try:
            tasks = await todoist.open_tasks(day)
        except todoist.TodoistError as e:
            log.warning("Scoring without Todoist: %s", e)
    block = todoist.prompt_block(day, tasks) if tasks else ""

    result = await scoring.score_day(day, full_text, block, existing["score"] if existing else None)
    entry = db.save_entry(day, full_text, result, scoring.current_model(), source)
    if tasks:
        await todoist.close_matched(day, result.get("completed_task_ids", []), tasks)
    return entry, existing["score"] if existing else None


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

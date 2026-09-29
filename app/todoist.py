"""Todoist integration (API v1).

- When a day is logged, open tasks from the chosen projects go into the AI prompt. Tasks
  the AI says the note completed are closed in Todoist and recorded for that day.
- A background sync records tasks you complete directly in Todoist, so History can show
  everything finished on a day. Those are for display only; they don't change the score.
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone

import httpx2 as httpx

from . import db, prefs

log = logging.getLogger("dayscore.todoist")

API = os.environ.get("TODOIST_API_URL", "https://api.todoist.com/api/v1").rstrip("/")
MAX_TASKS_IN_PROMPT = 150
SYNC_EVERY = 15 * 60


class TodoistError(Exception):
    pass


# ---------- config ----------

def token() -> str:
    return prefs.get("todoist_token")


def project_ids() -> list[str]:
    return [p for p in prefs.get("todoist_projects").split(",") if p]


def enabled() -> bool:
    return bool(token() and project_ids())


def project_names() -> dict[str, str]:
    return json.loads(db.get_setting("todoist.project_names", "{}"))


# ---------- HTTP ----------

async def _request(method: str, path: str, tok: str | None = None, **params) -> dict:
    tok = tok or token()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.request(method, f"{API}/{path}", params=params or None,
                                        headers={"Authorization": f"Bearer {tok}"})
    except httpx.HTTPError as e:
        raise TodoistError("Couldn't reach Todoist.") from e
    if resp.status_code in (401, 403):
        raise TodoistError("Todoist didn't accept this token. Copy it again from Todoist's settings.")
    if resp.status_code == 429:
        raise TodoistError("Todoist is rate limiting us, try again in a minute.")
    if resp.status_code >= 400:
        raise TodoistError(f"Todoist returned an error ({resp.status_code}).")
    return resp.json() if resp.content else {}


async def _all(path: str, field: str, tok: str | None = None, **params) -> list[dict]:
    """Follow next_cursor through every page."""
    items, cursor = [], None
    while True:
        page = await _request("GET", path, tok, limit=200, **params, **({"cursor": cursor} if cursor else {}))
        items += page.get(field, [])
        cursor = page.get("next_cursor")
        if not cursor:
            return items


async def fetch_projects(tok: str | None = None) -> list[dict]:
    projects = await _all("projects", "results", tok)
    names = {p["id"]: p["name"] for p in projects}
    db.set_setting("todoist.project_names", json.dumps(names, ensure_ascii=False))
    return [{"id": p["id"], "name": p["name"], "is_inbox": bool(p.get("inbox_project"))} for p in projects]


# ---------- task snapshots ----------

def _snapshot(task: dict) -> dict:
    """The task fields we keep for History."""
    due = task.get("due") or {}
    deadline = task.get("deadline") or {}
    return {
        "id": task["id"],
        "content": task.get("content", ""),
        "description": task.get("description", ""),
        "priority": task.get("priority", 1),          # API: 4 = highest (the app's "P1")
        "labels": task.get("labels") or [],
        "due": due.get("date", "")[:10] if due else "",
        "due_string": due.get("string", "") if due else "",
        "recurring": bool(due.get("is_recurring")) if due else False,
        "deadline": deadline.get("date", "")[:10] if deadline else "",
        "project_id": task.get("project_id", ""),
        "project": project_names().get(task.get("project_id", ""), ""),
        "url": f"https://app.todoist.com/app/task/{task['id']}",
    }


async def open_tasks(day: date) -> list[dict]:
    """Open tasks in the chosen projects, minus any DayScore already closed for this day
    (a recurring task stays open after closing, and must not be closed twice)."""
    if not project_ids():
        return []
    if not project_names():
        await fetch_projects()
    already = {row["task_id"] for row in db.todoist_for_day(day) if row["source"] == "dayscore"}
    tasks = []
    for pid in project_ids():
        tasks += await _all("tasks", "results", project_id=pid)
    tasks = [_snapshot(t) for t in tasks if t["id"] not in already]
    # Most relevant first: due soonest, then highest priority.
    tasks.sort(key=lambda t: (t["due"] or "9999", -t["priority"]))
    return tasks[:MAX_TASKS_IN_PROMPT]


def prompt_block(day: date, tasks: list[dict]) -> str:
    lines = []
    for t in tasks:
        extra = [f"project: {t['project']}", f"priority: p{5 - t['priority']}"]
        if t["due"]:
            extra.append("due: TODAY" if t["due"] == day.isoformat() else f"due: {t['due']}")
        desc = f" – {t['description'][:150]}" if t["description"] else ""
        lines.append(f"[{t['id']}] {t['content']}{desc} ({', '.join(extra)})")
    return "<todoist_open_tasks>\n" + "\n".join(lines) + "\n</todoist_open_tasks>"


async def close_matched(day: date, ids: list[str], tasks: list[dict]) -> list[dict]:
    """Close the tasks the AI matched (only real IDs from the list we gave it)."""
    by_id = {t["id"]: t for t in tasks}
    matched = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def close(task: dict) -> None:
        try:
            await _request("POST", f"tasks/{task['id']}/close")
            status = "done"
        except TodoistError as e:
            log.warning("Couldn't close Todoist task %s: %s", task["id"], e)
            status = "failed"
        db.save_todoist_done(task["id"], day, now, "dayscore", status, task)

    await asyncio.gather(*(close(t) for t in matched))
    if matched:
        log.info("Closed %d Todoist task(s) for %s", len(matched), day)
    return matched


# ---------- background sync of tasks completed in Todoist ----------

async def sync(days: int = 30) -> int:
    """Record tasks completed in the chosen projects during the last `days` days."""
    if not enabled():
        return 0
    await fetch_projects()
    tz = prefs.tz()
    start = datetime.combine(datetime.now(tz).date() - timedelta(days=days), time.min, tz)
    items = await _all("tasks/completed/by_completion_date", "items",
                       since=start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       until=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    wanted = set(project_ids())
    added = 0
    for item in items:
        if item.get("project_id") not in wanted or not item.get("completed_at"):
            continue
        completed = datetime.fromisoformat(item["completed_at"].replace("Z", "+00:00"))
        if db.todoist_closed_by_dayscore_near(item["id"], completed):
            continue  # DayScore closed it (maybe filed under yesterday's note) - don't list twice
        day = completed.astimezone(tz).date()
        added += db.save_todoist_done(item["id"], day, completed.isoformat(timespec="seconds"),
                                      "todoist", "done", _snapshot(item), replace=False)
    return added


async def sync_loop() -> None:
    while True:
        try:
            if enabled():
                added = await sync(days=int(db.get_setting("todoist.sync_days", "30")))
                db.set_setting("todoist.sync_days", "2")  # full month once, then just recent days
                if added:
                    log.info("Synced %d completed Todoist task(s)", added)
        except TodoistError as e:
            log.warning("Todoist sync failed: %s", e)
        except Exception:
            log.exception("Todoist sync crashed")
        await asyncio.sleep(SYNC_EVERY)

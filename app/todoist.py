"""Todoist integration (API v1).

- When a day is logged, open tasks from the chosen projects go into the AI prompt. Tasks
  the AI says the note completed are suggested for confirmation (or ticked off right away if
  confirmation is off); ticked-off tasks are recorded for that day.
- Tasks the note explicitly asks for later are added to the Todoist Inbox.
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone

import httpx2 as httpx

from . import db, prefs

log = logging.getLogger("dayscore.todoist")

API = os.environ.get("TODOIST_API_URL", "https://api.todoist.com/api/v1").rstrip("/")
MAX_TASKS_IN_PROMPT = 150


class TodoistError(Exception):
    pass


# ---------- config ----------

def token() -> str:
    return prefs.get("todoist_token")


def project_ids() -> list[str]:
    return [p for p in prefs.get("todoist_projects").split(",") if p]


def enabled() -> bool:
    """Matching finished tasks needs a token and at least one watched project."""
    return bool(token() and project_ids())


def confirm_first() -> bool:
    return prefs.get("todoist_confirm") != "0"


def can_create() -> bool:
    return bool(token()) and prefs.get("todoist_create") != "0"


def project_names() -> dict[str, str]:
    return json.loads(db.get_setting("todoist.project_names", "{}"))


# ---------- HTTP ----------

async def _request(method: str, path: str, tok: str | None = None, body: dict | None = None, **params) -> dict:
    tok = tok or token()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.request(method, f"{API}/{path}", params=params or None, json=body,
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
    # Skip tasks already ticked off or already suggested for this day (whatever the answer was).
    already = {row["task_id"] for row in db.todoist_for_day(day) if row["source"] == "dayscore"}
    already |= db.suggested_task_ids(day)
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


def created_block(day: date) -> str:
    """Tasks already created from this day's note, so an added note doesn't create them twice."""
    items = db.created(day)
    if not items:
        return ""
    return "<already_added_todos>\n" + "\n".join(f"- {c['content']}" for c in items) + "\n</already_added_todos>"


def matched(ids: list[str], tasks: list[dict]) -> list[dict]:
    """Only real IDs from the list the AI was given."""
    by_id = {t["id"]: t for t in tasks}
    return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]


def suggest(day: date, found: list[dict]) -> list[dict]:
    """Remember matched tasks as suggestions that wait for the owner's OK."""
    for task in found:
        db.add_suggestion(day, task)
    return db.suggestions(day, status="pending")


async def decide(day: date, accept: list[int], reject: list[int]) -> dict:
    """Tick off the accepted suggestions in Todoist; forget the rejected ones for this day."""
    pending = {s["suggestion_id"]: s for s in db.suggestions(day, status="pending")}
    internal = ("suggestion_id", "day", "status", "selected")
    to_close = [{k: v for k, v in pending[i].items() if k not in internal} for i in accept if i in pending]
    accepted_ids = [i for i in accept if i in pending]
    results = await close_matched(day, [s["id"] for s in to_close], to_close)
    for i in accepted_ids:
        db.set_suggestion(i, status="accepted")
    for i in reject:
        if i in pending:
            db.set_suggestion(i, status="rejected")
    return {"closed": [r["content"] for r in results if r["ok"]],
            "failed": [r["content"] for r in results if not r["ok"]],
            "rejected": len([i for i in reject if i in pending])}


async def create_todos(day: date, todos: list[dict]) -> list[dict]:
    """Add tasks the note explicitly asked for to the Todoist Inbox (no project = Inbox)."""
    seen = {c["content"].strip().lower() for c in db.created(day)}
    made = []
    for todo in todos:
        content = (todo.get("content") or "").strip()[:500]
        if not content or content.lower() in seen:
            continue
        due = (todo.get("due_date") or "").strip()
        body = {"content": content}
        if len(due) == 10 and due[4] == "-" and due[7] == "-":
            body["due_date"] = due
        else:
            due = ""
        try:
            task = await _request("POST", "tasks", body=body)
        except TodoistError as e:
            log.warning("Couldn't add a task to Todoist: %s", e)
            continue
        db.add_created(day, task.get("id", ""), content, due)
        seen.add(content.lower())
        made.append({"content": content, "due": due})
    if made:
        log.info("Added %d task(s) to the Todoist Inbox", len(made))
    return made


async def close_matched(day: date, ids: list[str], tasks: list[dict]) -> list[dict]:
    """Close the given tasks in Todoist and record them for the day. Each returned task has
    "ok": whether Todoist accepted it."""
    matched_tasks = matched(ids, tasks)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def close(task: dict) -> dict:
        try:
            await _request("POST", f"tasks/{task['id']}/close")
            status = "done"
        except TodoistError as e:
            log.warning("Couldn't close Todoist task %s: %s", task["id"], e)
            status = "failed"
        db.save_todoist_done(task["id"], day, now, "dayscore", status, task)
        return {**task, "ok": status == "done"}

    results = await asyncio.gather(*(close(t) for t in matched_tasks))
    if results:
        log.info("Closed %d of %d Todoist task(s) for %s", sum(r["ok"] for r in results), len(results), day)
    return list(results)

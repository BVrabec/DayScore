"""Turns a free-text day summary (Slovenian or English) into a 0-100 score.

Uses Claude directly (Anthropic API) or any model with structured outputs via OpenRouter.
"""

import json
import logging
import os
from datetime import date
from typing import Literal

import anthropic
import httpx2 as httpx
from pydantic import BaseModel

from . import db, prefs

log = logging.getLogger("dayscore.scoring")

CATEGORIES = ["work", "projects", "learning", "home", "health", "social", "errands", "leisure"]

Category = Literal["work", "projects", "learning", "home", "health", "social", "errands", "leisure"]


class Activity(BaseModel):
    text: str
    category: Category


class DayScore(BaseModel):
    score: int
    title: str
    summary: str
    activities: list[Activity]
    reason: str
    tip: str
    completed_task_ids: list[str] = []


SYSTEM_PROMPT = """You score how productive someone's day was, from 0 to 100, based on their own short \
end-of-day note. The note may be written in any language (or a mix). Always answer in English.

How to score:
- Judge effort and follow-through, not just the number of items. Hard, boring or long tasks count \
for more than quick ones. Finishing something counts for more than starting it.
- Anything done instead of mindless scrolling or procrastination counts: work, study, side projects, \
fixing things around the house or home server, chores, errands, exercise, cooking, helping someone.
- Intentional rest, sleep and time with people are healthy, not failures. A deliberate rest day can \
still score 50-65. Hours lost to doomscrolling, bingeing or avoiding tasks pull the score down.
- Depth beats quantity: one long, focused session on something meaningful (building a project, \
studying, creating) can make a very productive day on its own. Never lower the score just \
because the list is short.
- Workdays: the <day> tag says whether it was a workday. On a workday their job already takes \
most of the day, so productive things done after work (side projects, learning, chores, sport) \
take extra effort and deserve extra credit, and less volume is expected than on a free day. \
On free days expect a bit more. If they describe their job work, count it as solid work.
- If the person mentions they were sick, travelling or had a hard day, judge against what was \
realistic that day.
- If <personal_priorities> is given, follow it: it says what counts for this person and wins \
over the general guidance where they differ.
- Be consistent: similar days must get similar scores. Use the recent days listed below as your \
calibration anchor. Do not inflate. Most ordinary days land between 45 and 75.

Score bands:
- 90-100: exceptional. Big, hard things finished, long focused stretches, almost no wasted time.
- 75-89: very productive. Several meaningful things done with real effort.
- 60-74: solid. Useful work and tasks done, some slack.
- 40-59: mixed. A few things done, noticeable wasted time.
- 20-39: low. Very little done, mostly drifting.
- 0-19: nothing meaningful done.

Fields to return:
- score: integer 0-100.
- title: a 2-6 word headline for the day, like "Server fixes and a long run".
- summary: 1-2 sentences in English, second person ("You ...").
- activities: each distinct thing they did, as a short English phrase (max ~8 words), with the \
best-fitting category. Categories: work (job), projects (side projects, homelab, tinkering), \
learning (study, courses, reading to learn), home (chores, repairs, cooking, cleaning), \
health (exercise, sleep, doctor), social (friends, family, partner), errands (admin, shopping, \
paperwork), leisure (games, shows, hobbies, relaxing, scrolling).
- reason: one or two sentences explaining the score, specific to what they wrote.
- tip: one short, concrete, friendly suggestion for tomorrow.
- completed_task_ids: if <todoist_open_tasks> is given, the [id]s of tasks the note clearly says \
were finished that day, even if worded differently or in another language. Leave out anything \
uncertain or only started. Use [] if none or if there is no task list.

To-do tasks (only if <todoist_open_tasks> is given):
- Tasks marked "due: TODAY" that the note doesn't show as done may lower the score a little: \
1-3 points each, a bit more for p1/p2 priority, at most about 10 points in total. Never lower \
the score for tasks due on other days. Doing tasks from the list is normal productive work."""


class ScoringError(Exception):
    pass


# ---------- providers ----------

OPENROUTER_URL = os.environ.get("OPENROUTER_API_URL", "https://openrouter.ai/api/v1").rstrip("/")

# JSON schema for OpenRouter's strict structured outputs (every object closed, every field required).
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score", "title", "summary", "activities", "reason", "tip", "completed_task_ids"],
    "properties": {
        "score": {"type": "integer", "description": "0-100"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "activities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "category"],
                "properties": {
                    "text": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
            },
        },
        "reason": {"type": "string"},
        "tip": {"type": "string"},
        "completed_task_ids": {"type": "array", "items": {"type": "string"}},
    },
}

NOT_CONNECTED = "The AI isn't connected yet. Add your API key in Settings."

_clients: dict[str, anthropic.AsyncAnthropic] = {}


def current_model() -> str:
    """What gets stored with each entry, e.g. 'claude-haiku-4-5' or 'openrouter:google/gemini-2.5-flash'."""
    if prefs.get("ai_provider") == "openrouter":
        return f"openrouter:{prefs.get('openrouter_model')}"
    return prefs.get("ai_model")


def _anthropic_client() -> anthropic.AsyncAnthropic:
    key = prefs.get("anthropic_api_key")
    if not key:
        raise ScoringError(NOT_CONNECTED)
    if key not in _clients:  # a new key from Settings gets a fresh client
        _clients.clear()
        _clients[key] = anthropic.AsyncAnthropic(api_key=key, timeout=60.0)
    return _clients[key]


async def check_api_key(provider: str, key: str) -> None:
    """Raise ScoringError if the key doesn't work. Neither check costs anything."""
    if provider == "openrouter":
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{OPENROUTER_URL}/key", headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as e:
            raise ScoringError("Couldn't reach OpenRouter. Check the server's internet connection.") from e
        if resp.status_code == 401:
            raise ScoringError("OpenRouter didn't accept this key. Check that you copied all of it.")
        if resp.status_code != 200:
            raise ScoringError(f"OpenRouter returned an error ({resp.status_code}).")
        return

    client = anthropic.AsyncAnthropic(api_key=key, timeout=15.0, max_retries=1)
    try:
        await client.models.list(limit=1)
    except anthropic.AuthenticationError as e:
        raise ScoringError("This API key isn't valid. Check that you copied all of it.") from e
    except anthropic.PermissionDeniedError as e:
        raise ScoringError("This API key doesn't have permission to use the API.") from e
    except anthropic.APIStatusError as e:
        raise ScoringError(f"The AI provider returned an error ({e.status_code}).") from e
    except anthropic.APIConnectionError as e:
        raise ScoringError("Couldn't reach the AI provider. Check the server's internet connection.") from e


def _build_prompt(day: date, text: str, tasks_block: str = "") -> str:
    parts = []
    priorities = db.get_setting("priorities", "").strip()
    if priorities:
        parts.append(f"<personal_priorities>\n{priorities}\n</personal_priorities>")

    workdays = prefs.workdays()
    kind = lambda d: "workday" if d.weekday() in workdays else "free day"

    recent = db.recent_entries(before=day, limit=14)
    if recent:
        lines = []
        for e in recent:
            d = date.fromisoformat(e["day"])
            lines.append(f"{e['day']} ({d:%a}, {kind(d)}): {e['score']} - {e['title']} ({e['summary']})")
        parts.append("<recent_days>\n" + "\n".join(lines) + "\n</recent_days>")

    if tasks_block:
        parts.append(tasks_block)
    workday = "yes" if day.weekday() in workdays else "no"
    parts.append(f"<day date=\"{day.isoformat()}\" weekday=\"{day:%A}\" workday=\"{workday}\">\n{text}\n</day>")
    return "\n\n".join(parts)


async def _score_anthropic(prompt: str) -> DayScore:
    client = _anthropic_client()
    model = prefs.get("ai_model")
    options = {}
    if "haiku" in model:
        # Haiku still accepts sampling params; temperature 0 keeps scores repeatable.
        # Newer models reject non-default temperature, so only send it here.
        options["extra_body"] = {"temperature": 0}
    else:
        # Newer models think before answering; this task doesn't need much.
        options["output_config"] = {"effort": "low"}

    try:
        response = await client.messages.parse(
            model=model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=DayScore,
            **options,
        )
    except anthropic.AuthenticationError as e:
        raise ScoringError("The AI key is no longer valid. Update it in Settings.") from e
    except anthropic.RateLimitError as e:
        raise ScoringError("Rate limited by the AI provider, try again in a minute.") from e
    except anthropic.APIStatusError as e:
        raise ScoringError(f"AI provider error ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ScoringError("Could not reach the AI provider.") from e

    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise ScoringError(f"The AI did not return a score (stop reason: {response.stop_reason}).")
    return response.parsed_output


async def _score_openrouter(prompt: str) -> DayScore:
    key = prefs.get("openrouter_api_key")
    if not key:
        raise ScoringError(NOT_CONNECTED)
    model = prefs.get("openrouter_model")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "day_score", "strict": True, "schema": SCHEMA},
        },
        # Only route to providers that honour the JSON schema (and temperature).
        "provider": {"require_parameters": True},
        "temperature": 0,
        "max_tokens": 4000,
    }
    headers = {"Authorization": f"Bearer {key}", "X-Title": "DayScore"}
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{OPENROUTER_URL}/chat/completions", json=body, headers=headers)
    except httpx.HTTPError as e:
        raise ScoringError("Could not reach OpenRouter.") from e

    try:
        data = resp.json()
    except ValueError:
        data = {}
    error = (data.get("error") or {}).get("message", "")
    if resp.status_code == 401:
        raise ScoringError("The OpenRouter key is no longer valid. Update it in Settings.")
    if resp.status_code == 402:
        raise ScoringError("Your OpenRouter account is out of credits.")
    if resp.status_code == 429:
        raise ScoringError("Rate limited by OpenRouter, try again in a minute.")
    if resp.status_code != 200 or error:
        if "No endpoints found" in error:
            raise ScoringError(f"The model {model} can't return structured scores. Pick another model in Settings.")
        raise ScoringError(f"OpenRouter error ({resp.status_code}): {error or 'unknown error'}")

    try:
        content = data["choices"][0]["message"]["content"] or ""
        # Some models still wrap JSON in a ```json fence.
        content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return DayScore.model_validate(json.loads(content))
    except (KeyError, IndexError, ValueError) as e:
        log.warning("Unparseable OpenRouter reply from %s: %.300s", model, data)
        raise ScoringError("The AI returned an answer that couldn't be read. Try again, or pick another model.") from e


async def score_day(day: date, text: str, tasks_block: str = "") -> dict:
    prompt = _build_prompt(day, text, tasks_block)
    if prefs.get("ai_provider") == "openrouter":
        result = await _score_openrouter(prompt)
    else:
        result = await _score_anthropic(prompt)
    data = result.model_dump()
    data["score"] = max(0, min(100, data["score"]))
    return data

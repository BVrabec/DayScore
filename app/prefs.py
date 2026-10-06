"""User-editable configuration, stored in the database and editable in Settings.

A value saved in the UI always wins. If nothing was saved, the matching environment
variable is used, then the built-in default. An empty saved value means "off"
(e.g. no evening reminder), which is different from "never saved".
"""

import os
from datetime import time
from zoneinfo import ZoneInfo

from . import db

# key -> (environment variable, default)
DEFAULTS = {
    "ai_provider": ("AI_PROVIDER", "anthropic"),          # anthropic | openrouter | local
    "anthropic_api_key": ("ANTHROPIC_API_KEY", ""),
    "ai_model": ("AI_MODEL", "claude-haiku-4-5"),
    "openrouter_api_key": ("OPENROUTER_API_KEY", ""),
    "openrouter_model": ("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5"),
    "local_url": ("LOCAL_AI_URL", ""),                       # OpenAI-compatible server, e.g. Ollama
    "local_api_key": ("LOCAL_AI_KEY", ""),                   # usually empty
    "local_model": ("LOCAL_AI_MODEL", ""),
    "todoist_token": ("TODOIST_API_TOKEN", ""),
    "todoist_projects": ("TODOIST_PROJECTS", ""),           # comma-separated project IDs
    "todoist_confirm": ("TODOIST_CONFIRM", "1"),            # ask before ticking tasks off
    "todoist_create": ("TODOIST_CREATE", "1"),              # add tasks mentioned for later to the Inbox
    "telegram_token": ("TELEGRAM_BOT_TOKEN", ""),
    "telegram_user_id": ("TELEGRAM_ALLOWED_USER_ID", ""),
    "telegram_bot_username": (None, ""),
    "telegram_user_name": (None, ""),
    "telegram_link_code": (None, ""),
    "timezone": ("TZ", "UTC"),
    "late_entry_until": ("LATE_ENTRY_UNTIL", "12:00"),
    "workdays": ("WORKDAYS", "0,1,2,3,4"),                    # Monday = 0 ... Sunday = 6
    "morning_reminder": ("MORNING_REMINDER", "09:00"),
    "evening_reminder": ("EVENING_REMINDER", "21:30"),
    "weekly_summary": ("WEEKLY_SUMMARY", "1"),               # Sunday evening recap on Telegram
}

# Yearly costs assume one note a day (~2k tokens in, ~300 out).
MODELS = {
    "anthropic": {
        "claude-haiku-4-5": "Claude Haiku 4.5 – recommended (about $1–2 a year)",
        "claude-sonnet-5-5": "Claude Sonnet 5.5 – a bit smarter (about $3 a year)",
    },
    "local": {},   # whatever the server offers
    "openrouter": {
        "anthropic/claude-haiku-4.5": "Claude Haiku 4.5 – recommended (about $1–2 a year)",
        "anthropic/claude-sonnet-5.5": "Claude Sonnet 5.5 – a bit smarter (about $3 a year)",
        "google/gemini-2.5-flash": "Gemini 2.5 Flash – cheaper (about $0.50 a year)",
        "deepseek/deepseek-v3.2": "DeepSeek V3.2 – cheapest (about $0.25 a year)",
    },
}

# Models that still accept a temperature (0 keeps their scores repeatable); newer ones refuse it.
TEMPERATURE_MODELS = {"claude-haiku-4-5"}

AI_KEYS = {"anthropic": ("anthropic_api_key", "ai_model"), "openrouter": ("openrouter_api_key", "openrouter_model"),
           "local": ("local_api_key", "local_model")}


def ai_configured() -> bool:
    provider = get("ai_provider")
    if provider == "local":
        return bool(get("local_url") and get("local_model"))
    key_name, _ = AI_KEYS.get(provider, AI_KEYS["anthropic"])
    return bool(get(key_name))


def get(key: str) -> str:
    saved = db.get_setting(f"cfg.{key}")
    if saved is not None:
        return saved
    env_name, default = DEFAULTS[key]
    return (os.environ.get(env_name) if env_name else None) or default


def put(key: str, value: str) -> None:
    assert key in DEFAULTS
    db.set_setting(f"cfg.{key}", value.strip())


def _time(value: str) -> time | None:
    value = value.strip()
    if not value:
        return None
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def workdays() -> set[int]:
    """Weekday numbers (Monday = 0) the person works at their job."""
    return {int(d) for d in get("workdays").split(",") if d.strip().isdigit() and 0 <= int(d) <= 6}


def late_entry_until() -> time:
    return _time(get("late_entry_until")) or time(12, 0)


def morning_reminder() -> time | None:
    return _time(get("morning_reminder"))


def evening_reminder() -> time | None:
    return _time(get("evening_reminder"))


def telegram_user_id() -> int | None:
    value = get("telegram_user_id")
    return int(value) if value.strip().lstrip("-").isdigit() else None


def mask(secret: str) -> str:
    return f"••••{secret[-4:]}" if len(secret) > 8 else ("••••" if secret else "")

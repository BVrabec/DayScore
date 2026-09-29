"""Server-level settings from environment variables.

Everything a user configures (AI key, Telegram, time zone, reminders...) lives in
prefs.py and can be changed in the web UI; env vars only provide its first defaults.
"""

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str
    data_dir: Path
    port: int
    # Optional fixed password; if unset you create one in the browser on first visit.
    app_password: str
    disable_auth: bool
    secure_cookies: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dayscore.db"


def load() -> Settings:
    env = os.environ.get
    return Settings(
        app_name=env("APP_NAME", "DayScore"),
        data_dir=Path(env("DATA_DIR", "./data")),
        port=int(env("PORT", "8000")),
        app_password=env("APP_PASSWORD", ""),
        disable_auth=_bool(env("DISABLE_AUTH", "false")),
        secure_cookies=_bool(env("SECURE_COOKIES", "false")),
    )


settings = load()

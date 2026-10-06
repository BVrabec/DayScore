"""Server-level settings from environment variables.

Everything a user configures (AI key, Telegram, time zone, reminders...) lives in
prefs.py and can be changed in the web UI; env vars only provide its first defaults.
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str
    accent: str          # color theme: "orchid" (violet/pink, default) or "green"
    data_dir: Path
    port: int
    # Optional fixed password; if unset you create one in the browser on first visit.
    app_password: str
    disable_auth: bool
    secure_cookies: bool
    # Encryption key for the data at rest (DAYSCORE_KEY or a file in DAYSCORE_KEY_FILE). It must
    # live outside the data volume: whoever has only the volume then sees encrypted bytes.
    data_key: str
    # Scratch space for plaintext copies (backup snapshots, restores, rclone's config). In Docker
    # this is a RAM-only tmpfs, so decrypted data never touches a disk.
    tmp_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dayscore.db"


def _data_key(env) -> str:
    key = env("DAYSCORE_KEY", "").strip()
    path = env("DAYSCORE_KEY_FILE", "").strip()
    if not key and path:
        key = Path(path).read_text().strip()
    return key


def load() -> Settings:
    env = os.environ.get
    return Settings(
        app_name=env("APP_NAME", "DayScore"),
        accent=env("APP_ACCENT", "orchid").strip().lower(),
        data_dir=Path(env("DATA_DIR", "./data")),
        port=int(env("PORT", "8000")),
        app_password=env("APP_PASSWORD", ""),
        disable_auth=_bool(env("DISABLE_AUTH", "false")),
        secure_cookies=_bool(env("SECURE_COOKIES", "false")),
        data_key=_data_key(env),
        tmp_dir=Path(env("TMP_DIR", "") or Path(tempfile.gettempdir()) / "dayscore"),
    )


settings = load()

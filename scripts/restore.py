"""Replace the current database with a backup, then restart DayScore.

    docker compose cp ./dayscore-2026-01-31_120000.db dayscore:/data/restore.db
    docker compose exec dayscore python -m scripts.restore /data/restore.db
    docker compose restart dayscore

A backup that's already inside (in /data/backups) can be restored directly by its path.
"""

import sqlite3
import sys
from pathlib import Path

from app import db
from app.config import settings


def main() -> None:
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_file():
        raise SystemExit("Usage: python -m scripts.restore /data/restore.db")
    db.init()
    with sqlite3.connect(sys.argv[1]) as src, sqlite3.connect(settings.db_path) as dst:
        src.backup(dst)
    print("Restored. Now run: docker compose restart dayscore")


if __name__ == "__main__":
    main()

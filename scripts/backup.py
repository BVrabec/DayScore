"""Save a consistent copy of the database to /data/backups (safe while DayScore is running).

    docker compose exec dayscore python -m scripts.backup
    docker compose cp dayscore:/data/backups ./dayscore-backups     # copy them out
"""

import sqlite3
from datetime import datetime

from app import db
from app.config import settings


def main() -> None:
    db.init()
    folder = settings.data_dir / "backups"
    folder.mkdir(exist_ok=True)
    target = folder / f"dayscore-{datetime.now():%Y-%m-%d_%H%M%S}.db"
    with sqlite3.connect(settings.db_path) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    print(f"Backup saved: {target}")


if __name__ == "__main__":
    main()

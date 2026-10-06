"""Shared test setup: a throwaway data folder with encryption on, and a fake AI.

The environment must be set before the app is imported (settings are read once at import).
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="dayscore-tests-"))
os.environ.update({
    "DATA_DIR": str(ROOT / "data"),
    "TMP_DIR": str(ROOT / "tmp"),
    "DAYSCORE_KEY": "test-key-not-for-production",
    "TZ": "UTC",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, main, scoring, security, service, telegram  # noqa: E402
from app.config import settings  # noqa: E402

auth.ITERATIONS = 1000   # fast password hashing in tests
main.FAIL_DELAY = 0

PASSWORD = "correct horse"


def fake_result(text: str) -> dict:
    words = len(text.split())
    return {
        "score": min(95, 40 + words), "title": f"{words} words", "summary": "You did things.",
        "activities": [{"text": "something", "category": "work"}], "reason": "Because.",
        "tip": "Rest.", "completed_task_ids": [], "new_todos": [],
    }


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Every test starts with an empty, encrypted database and a fake AI."""
    shutil.rmtree(ROOT / "data", ignore_errors=True)
    shutil.rmtree(ROOT / "tmp", ignore_errors=True)
    security._failures.clear()
    service._day_locks.clear()
    auth._setup_code = ""
    main.LOCKED = None
    telegram.status.update(state="stopped", detail="")
    db.init()

    async def score_day(day, text, tasks_block="", earlier_score=None):
        await asyncio.sleep(0.05)   # like a real API call: other requests can run meanwhile
        return fake_result(text)

    monkeypatch.setattr(scoring, "score_day", score_day)
    yield


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def set_up(c: TestClient, password: str = PASSWORD) -> None:
    r = c.post("/api/setup", json={"password": password, "setup_code": auth.setup_code()})
    assert r.status_code == 200, r.text


def unlock(c: TestClient, password: str = PASSWORD) -> None:
    assert c.post("/api/unlock", json={"password": password}).status_code == 200


@pytest.fixture
def user(client):
    """A set-up install with a signed-in browser."""
    set_up(client)
    return client


def data_dir() -> Path:
    return settings.data_dir

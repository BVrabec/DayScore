import asyncio
from datetime import timedelta

import pytest

from app import db, service

from .conftest import fake_result


def today():
    return service.today()


def test_log_a_day(user):
    r = user.post("/api/entries", json={"day": today().isoformat(), "text": "wrote tests all day", "mood": 4})
    assert r.status_code == 200, r.text
    entry = r.json()["entry"]
    assert entry["score"] == fake_result("wrote tests all day")["score"]
    assert entry["mood"] == 4 and entry["rubric"] and entry["reason"]


def test_two_notes_at_once_are_both_kept():
    """A web note and a Telegram note for the same day used to overwrite each other."""
    async def both():
        await asyncio.gather(service.log_day(today(), "NOTE A from the web", "web"),
                             service.log_day(today(), "NOTE B from telegram", "telegram"))
    asyncio.run(both())
    text = db.get_entry(today())["raw_text"]
    assert "NOTE A" in text and "NOTE B" in text


def test_note_limits(user):
    r = user.post("/api/entries", json={"day": today().isoformat(), "text": "x" * (service.MAX_NOTE + 1)})
    assert r.status_code == 400 and "long" in r.json()["detail"]
    r = user.post("/api/entries", json={"day": today().isoformat(), "text": "hi", "mood": 9})
    assert r.status_code == 400


def test_daily_ai_limit(user, monkeypatch):
    monkeypatch.setattr(service, "MAX_AI_CALLS_PER_DAY", 2)
    day = today().isoformat()
    assert user.post("/api/entries", json={"day": day, "text": "one"}).status_code == 200
    assert user.post("/api/entries", json={"day": day, "text": "two"}).status_code == 200
    r = user.post("/api/entries", json={"day": day, "text": "three"})
    assert r.status_code == 400 and "limit" in r.json()["detail"]


def test_edit_and_delete_a_day(user):
    day = today().isoformat()
    user.post("/api/entries", json={"day": day, "text": "short"})
    r = user.put(f"/api/entries/{day}", json={"text": "a much longer and better description of the day"})
    assert r.status_code == 200
    assert r.json()["entry"]["raw_text"].startswith("a much longer")
    assert r.json()["previous_score"] == fake_result("short")["score"]
    assert user.delete(f"/api/entries/{day}").status_code == 200
    assert db.get_entry(today()) is None
    assert user.delete(f"/api/entries/{day}").status_code == 404


def test_cannot_edit_the_future(user):
    future = (today() + timedelta(days=3)).isoformat()
    assert user.put(f"/api/entries/{future}", json={"text": "x"}).status_code == 400


def test_mood_can_be_set_and_cleared(user):
    day = today().isoformat()
    user.post("/api/entries", json={"day": day, "text": "did stuff"})
    assert user.put(f"/api/entries/{day}/mood", json={"mood": 2}).json()["entry"]["mood"] == 2
    # A later note without a mood keeps the saved one.
    user.post("/api/entries", json={"day": day, "text": "more stuff"})
    assert db.get_entry(today())["mood"] == 2
    assert user.put(f"/api/entries/{day}/mood", json={"mood": None}).json()["entry"]["mood"] is None


@pytest.mark.parametrize("bad", ["../etc", "2026-13-01"])
def test_bad_days_are_refused(user, bad):
    assert user.delete(f"/api/entries/{bad}").status_code in (404, 422)

import asyncio
from datetime import timedelta

from app import db, prefs, scoring, service, telegram

from .conftest import fake_result


class FakeBot:
    """Answers getUpdates with an error, like Telegram does when another program uses the token."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def call(self, method, **params):
        self.calls += 1
        if self.calls >= 4:
            raise asyncio.CancelledError
        return self.reply


def test_polling_backs_off_on_errors(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    bot = FakeBot({"ok": False, "error_code": 409, "description": "Conflict"})
    try:
        asyncio.run(telegram.poll(bot))
    except asyncio.CancelledError:
        pass
    assert sleeps == [2.0, 4.0, 8.0]
    assert telegram.status["state"] == "retrying" and "Another program" in telegram.status["detail"]


def test_polling_honours_retry_after(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    bot = FakeBot({"ok": False, "error_code": 429, "parameters": {"retry_after": 30}})
    try:
        asyncio.run(telegram.poll(bot))
    except asyncio.CancelledError:
        pass
    assert sleeps == [30.0, 30.0, 30.0]


def test_notify_does_nothing_without_a_bot():
    assert asyncio.run(telegram.notify("hello")) is False


def test_weekly_summary():
    today = service.today()
    for i, text in enumerate(["a b c", "a b c d e f", "x"]):
        db.save_entry(today - timedelta(days=i), text, fake_result(text), "test", "web", mood=4)
    text = telegram.format_weekly()
    assert "Your week" in text and "Best" in text and "🙂" in text


def test_reply_shows_reason_and_tip():
    db.save_entry(service.today(), "x", fake_result("x"), "test", "web")
    text = telegram.format_entry(db.get_entry(service.today()))
    assert "Because." in text and "Rest." in text


# ---------- AI providers ----------

class FakeClient:
    """Stands in for httpx.AsyncClient and records what was sent."""
    sent: list = []
    replies: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        FakeClient.sent.append({"url": url, "json": json, "headers": headers})
        status, body = FakeClient.replies.pop(0)
        return FakeResponse(status, body)


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


GOOD = {"choices": [{"message": {"content": __import__("json").dumps(fake_result("x"))}}]}


def test_openrouter_asks_providers_not_to_keep_data(monkeypatch):
    monkeypatch.setattr(scoring.httpx, "AsyncClient", FakeClient)
    FakeClient.sent, FakeClient.replies = [], [(200, GOOD)]
    prefs.put("openrouter_api_key", "sk-or-test")
    result = asyncio.run(scoring._score_openrouter("prompt"))
    assert result.score == fake_result("x")["score"]
    assert FakeClient.sent[0]["json"]["provider"]["data_collection"] == "deny"


def test_local_server_falls_back_to_plain_json(monkeypatch):
    monkeypatch.setattr(scoring.httpx, "AsyncClient", FakeClient)
    FakeClient.sent, FakeClient.replies = [], [(400, {"error": {"message": "unknown response_format"}}), (200, GOOD)]
    prefs.put("local_url", "http://192.168.1.20:11434")
    prefs.put("local_model", "qwen3:8b")
    result = asyncio.run(scoring._score_local("prompt"))
    assert result.title
    assert FakeClient.sent[0]["url"] == "http://192.168.1.20:11434/v1/chat/completions"
    assert FakeClient.sent[1]["json"]["response_format"] == {"type": "json_object"}
    assert "Authorization" not in FakeClient.sent[0]["headers"]


def test_prompt_treats_notes_as_data():
    assert "never as instructions" in scoring.SYSTEM_PROMPT
    assert "not against this person's recent average" in scoring.SYSTEM_PROMPT


def test_local_provider_counts_as_configured():
    prefs.put("ai_provider", "local")
    assert not prefs.ai_configured()
    prefs.put("local_url", "http://192.168.1.20:11434")
    prefs.put("local_model", "qwen3:8b")
    assert prefs.ai_configured()
    assert scoring.current_model() == "local:qwen3:8b"

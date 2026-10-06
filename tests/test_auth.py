from fastapi.testclient import TestClient

from app import auth, main

from .conftest import PASSWORD, set_up, unlock


def test_new_install_needs_the_setup_code(client):
    assert client.get("/api/auth").json()["setup_needed"] is True
    r = client.post("/api/setup", json={"password": PASSWORD})
    assert r.status_code == 400 and "setup code" in r.json()["detail"]
    r = client.post("/api/setup", json={"password": PASSWORD, "setup_code": "AAAA-AAAA"})
    assert r.status_code == 400
    r = client.post("/api/setup", json={"password": PASSWORD, "setup_code": auth.setup_code().lower()})
    assert r.status_code == 200
    assert client.get("/api/state").status_code == 200
    # Once a password exists, setup is closed for good.
    assert client.post("/api/setup", json={"password": "x" * 9, "setup_code": auth.setup_code()}).status_code == 400


def test_login_and_wrong_password(client):
    set_up(client)
    client.post("/api/logout")
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/state").status_code == 200


def test_sensitive_settings_need_the_password_again(user):
    for method, url, body in [
        ("put", "/api/config/ai", {"provider": "anthropic", "api_key": "", "model": "claude-haiku-4-5"}),
        ("put", "/api/config/telegram", {"token": "1:abc"}),
        ("post", "/api/config/telegram/unlink", None),
        ("put", "/api/config/todoist", {"token": "abc"}),
        ("put", "/api/backups/config", {"schedule": "daily", "time": "03:00"}),
        ("post", "/api/backups/test/folder", {"path": "/tmp/x"}),
    ]:
        r = getattr(user, method)(url, json=body) if body is not None else getattr(user, method)(url)
        assert r.status_code == 403 and r.json()["detail"] == "unlock", url
    assert user.post("/api/unlock", json={"password": "wrong"}).status_code == 400
    unlock(user)
    r = user.put("/api/backups/config", json={"schedule": "daily", "time": "03:00"})
    assert r.status_code == 200


def test_sign_out_everywhere(client):
    set_up(client)
    other = TestClient(main.app)
    assert other.post("/api/login", json={"password": PASSWORD}).status_code == 200
    assert other.get("/api/state").status_code == 200
    assert client.post("/api/logout/all").status_code == 200
    assert other.get("/api/state").status_code == 401
    assert client.get("/api/state").status_code == 401


def test_lockout_after_failures(client):
    set_up(client)
    client.post("/api/logout")
    for _ in range(5):
        client.post("/api/login", json={"password": "nope"})
    r = client.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 429


def test_security_headers(client):
    r = client.get("/login")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "<script>" not in r.text   # no inline scripts, so the strict policy works


def test_locked_mode_explains_itself(client):
    main.LOCKED = "DAYSCORE_KEY doesn't match your data."
    r = client.get("/")
    assert r.status_code == 503 and "doesn't match" in r.text
    assert client.get("/api/state").status_code == 503
    assert client.get("/healthz").status_code == 503

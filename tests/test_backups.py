import gzip
import json
import sqlite3

import pytest

from app import backup_targets as targets
from app import backups, db, service

from .conftest import PASSWORD, data_dir, unlock


def log(user, text="did a thing"):
    assert user.post("/api/entries", json={"day": service.today().isoformat(), "text": text}).status_code == 200


def configure(user, **cfg):
    unlock(user)
    body = {"schedule": "daily", "time": "03:00", **cfg}
    r = user.put("/api/backups/config", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_without_a_backup_password_only_this_server_keeps_an_encrypted_copy(user, tmp_path):
    log(user)
    configure(user, locations={"folder": {"enabled": True, "path": str(tmp_path / "nas")}})
    run = user.post("/api/backups/run").json()["runs"][0]
    res = {r["location"]: r for r in run["results"]}
    assert res["local"]["ok"] and res["local"]["name"].endswith(".enc")
    assert not res["folder"]["ok"] and "backup password" in res["folder"]["error"]
    # The server-key copy restores here without asking for a password.
    prep = user.post("/api/backups/restore/prepare", json={"kind": "local", "name": res["local"]["name"]})
    assert prep.status_code == 200 and prep.json()["days"] == 1


def test_every_copy_is_encrypted_with_the_backup_password(user, tmp_path):
    log(user)
    configure(user, password="backup-pass-1", locations={"folder": {"enabled": True, "path": str(tmp_path / "nas")}})
    run = user.post("/api/backups/run").json()["runs"][0]
    assert all(r["ok"] and r["name"].endswith(".enc") for r in run["results"]), run
    files = list((tmp_path / "nas").iterdir())
    assert files and all(f.read_bytes().startswith(backups.MAGIC) for f in files)
    with pytest.raises(backups.BackupError, match="Wrong backup password"):
        backups.decode(files[0].read_bytes(), "not-it")
    assert backups.decode(files[0].read_bytes(), "backup-pass-1").startswith(b"SQLite format 3")


def test_secrets_are_never_sent_back(user, tmp_path):
    cfg = configure(user, password="backup-pass-1",
                    locations={"smb": {"enabled": False, "server": "nas", "share": "b", "password": "smb-secret"}})
    blob = json.dumps(cfg)
    assert "backup-pass-1" not in blob and "smb-secret" not in blob
    assert cfg["config"]["password_set"] and cfg["config"]["locations"]["smb"]["password_set"]


def test_failed_location_is_retried_and_alerted(user, tmp_path, monkeypatch):
    sent = []

    async def notify(text):
        sent.append(text)
        return True
    from app import telegram
    monkeypatch.setattr(telegram, "notify", notify)
    log(user)
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file where the folder should be")
    configure(user, password="backup-pass-1", locations={"folder": {"enabled": True, "path": str(blocker)}})

    import asyncio
    run = asyncio.run(backups.run_backup("schedule"))
    assert {r["location"]: r["ok"] for r in run["results"]} == {"local": True, "folder": False}
    assert sent and "Backup problem" in sent[0]
    assert db.get_setting(backups.RETRY_KEY)

    # Next run with unchanged data: the local copy is up to date, the folder is tried again.
    blocker.unlink()
    run = asyncio.run(backups.run_backup("retry"))
    assert [r["location"] for r in run["results"]] == ["folder"] and run["results"][0]["ok"]
    assert not db.get_setting(backups.RETRY_KEY)
    # And now nothing is left to do.
    assert asyncio.run(backups.run_backup("schedule")).get("skipped")


def test_zip_bomb_is_refused(monkeypatch):
    monkeypatch.setattr(backups, "MAX_UNPACKED", 1024 * 1024)
    bomb = gzip.compress(b"\0" * (5 * 1024 * 1024))
    with pytest.raises(backups.BackupError, match="more than"):
        backups.decode(bomb, None)


def test_backup_from_a_newer_version_is_refused(tmp_path):
    newer = tmp_path / "n.db"
    conn = sqlite3.connect(newer)
    conn.executescript(db.SCHEMA_V1)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(backups.BackupError, match="newer DayScore"):
        backups.inspect(newer.read_bytes())


def test_restore_round_trip_with_safety_copy(user):
    log(user, "the day I want back")
    configure(user, password="backup-pass-1")
    name = user.post("/api/backups/run").json()["runs"][0]["results"][0]["name"]
    user.delete(f"/api/entries/{service.today().isoformat()}")
    assert db.get_entry(service.today()) is None

    prep = user.post("/api/backups/restore/prepare", json={"kind": "local", "name": name}).json()
    assert prep["days"] == 1
    assert user.post("/api/backups/restore/commit", json={"token": prep["token"], "account_password": "x"}).status_code == 400
    r = user.post("/api/backups/restore/commit", json={"token": prep["token"], "account_password": PASSWORD})
    assert r.status_code == 200 and r.json()["safety_copy"].endswith(".enc")
    assert "the day I want back" in db.get_entry(service.today())["raw_text"]
    # Restored data is encrypted on disk too.
    assert not (data_dir() / "dayscore.db").read_bytes().startswith(b"SQLite format 3")


def test_upload_restore_takes_the_password_from_a_header(user, tmp_path):
    log(user)
    configure(user, password="backup-pass-1")
    name = user.post("/api/backups/run").json()["runs"][0]["results"][0]["name"]
    data = (data_dir() / "backups" / name).read_bytes()
    r = user.post("/api/backups/restore/upload", content=data, headers={"X-Backup-Password": "wrong-one!"})
    # The saved backup password is tried too, so this still works...
    assert r.status_code == 200
    # ...but a file made with another password needs that password.
    other = backups.encode(backups.snapshot()[0], "other-pass-9")
    assert user.post("/api/backups/restore/upload", content=other).status_code == 400
    r = user.post("/api/backups/restore/upload", content=other, headers={"X-Backup-Password": "other-pass-9"})
    assert r.status_code == 200


def test_fresh_install_restore_needs_the_setup_code(client):
    from app import auth
    raw, _ = backups.snapshot()
    data = backups.encode(raw, "backup-pass-1")
    headers = {"X-Backup-Password": "backup-pass-1"}
    assert client.post("/api/backups/restore/upload", content=data, headers=headers).status_code == 400
    r = client.post("/api/backups/restore/upload", content=data, headers={**headers, "X-Setup-Code": auth.setup_code()})
    assert r.status_code == 200
    assert client.post("/api/backups/restore/commit", json={"token": r.json()["token"]}).status_code == 400
    r = client.post("/api/backups/restore/commit", json={"token": r.json()["token"], "setup_code": auth.setup_code()})
    assert r.status_code == 200


def test_too_big_upload_is_refused(user, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "MAX_UPLOAD", 1000)
    assert user.post("/api/backups/restore/upload", content=b"x" * 5000).status_code == 413


def test_old_plain_local_backups_get_encrypted(user):
    folder = data_dir() / "backups"
    folder.mkdir(exist_ok=True)
    raw, _ = backups.snapshot()
    (folder / "dayscore-2026-01-01_030000.db.gz").write_bytes(backups.encode(raw, None))
    assert backups.secure_local_backups() == 1
    names = [p.name for p in folder.iterdir()]
    assert names == ["dayscore-2026-01-01_030000.db.gz.enc"]


def test_retention_of_a_year_of_daily_backups():
    from datetime import datetime, timedelta
    start = datetime(2026, 1, 1, 3)
    names = [f"dayscore-{start + timedelta(days=i):%Y-%m-%d_%H%M%S}.db.gz.enc" for i in range(365)]
    kept = sorted(set(names) - set(backups.to_prune(names)))
    # 7 daily (25-31 Dec), plus weekly 13 and 20 Dec, plus monthly ends of Jul-Nov = 14.
    assert len(kept) == 14
    assert "dayscore-2026-12-20_030000.db.gz.enc" in kept and "dayscore-2026-07-31_030000.db.gz.enc" in kept


# ---------- location safety ----------

@pytest.mark.parametrize("conf, ok", [
    ("[gdrive]\ntype = drive\nscope = drive.file\ntoken = {\"access_token\":\"x\"}\n", True),
    ("[box]\ntype = dropbox\ntoken = {}\n", True),
    ("[evil]\ntype = sftp\nhost = example.com\nssh = sh -c 'id'\n", False),
    ("[evil]\ntype = drive\nservice_account_file = /data/dayscore.db\n", False),
    ("[a]\ntype = drive\n[b]\ntype = drive\n", False),
    ("[evil]\ntype = local\n", False),
    ("not a config", False),
])
def test_rclone_config_is_checked(conf, ok):
    if ok:
        assert targets.parse_rclone(conf)[0]
    else:
        with pytest.raises(targets.TargetError):
            targets.parse_rclone(conf)


def test_unsafe_rclone_config_cannot_be_saved(user):
    unlock(user)
    r = user.put("/api/backups/config", json={"schedule": "daily", "time": "03:00", "locations": {
        "rclone": {"enabled": True, "config": "[x]\ntype = sftp\nssh = id\n"}}})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "169.254.169.254", "::1", "0.0.0.0"])
def test_locations_cannot_point_at_this_server(host, monkeypatch):
    monkeypatch.delenv("BACKUP_ALLOW_LOOPBACK", raising=False)
    with pytest.raises(targets.TargetError):
        targets.check_host(host)


def test_lan_addresses_are_allowed(monkeypatch):
    monkeypatch.delenv("BACKUP_ALLOW_LOOPBACK", raising=False)
    targets.check_host("192.168.1.20")
    targets.check_host("10.0.0.5")

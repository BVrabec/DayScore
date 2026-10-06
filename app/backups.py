"""Backups: compressed, encrypted copies of the database, on a schedule, to one or more
locations, with smart keeping of old copies and a safe, verified restore.

A backup file is the whole SQLite database (days, settings, keys, 2FA), gzip-compressed and
encrypted with AES-256-GCM (key from a password via scrypt):
    dayscore-2026-10-05_030000.db.gz.enc

The password is the backup password from Settings. Until one is set, only the copy on this
server is made, encrypted with a key derived from DAYSCORE_KEY (so it can only be restored
here). Without DAYSCORE_KEY and without a backup password the local copy is plain, like the
database itself.

Plaintext copies (snapshots, restores being checked) only exist in TMP_DIR, a RAM-only tmpfs
in Docker.
"""

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import backup_targets as targets
from . import db, prefs
from .config import settings

log = logging.getLogger("dayscore.backups")

MAGIC = b"DAYSCORE-BACKUP-1\n"
NAME_RE = re.compile(r"^dayscore-(\d{4}-\d{2}-\d{2})_(\d{6})\.db\.gz(\.enc)?$")
PRE_RESTORE_RE = re.compile(r"^dayscore-pre-restore-\d{4}-\d{2}-\d{2}_\d{6}\.db\.gz(\.enc)?$")
KEEP = {"daily": 7, "weekly": 4, "monthly": 6}
CONFIG_KEY = "backup.config"
RUNS_KEY = "backup.runs"
RETRY_KEY = "backup.retry"
STAGE_TTL = 30 * 60
MAX_UNPACKED = 500 * 1024 * 1024   # a restore never unpacks more than this (zip-bomb guard)
RETRY_AFTER = 3600
MAX_RETRIES = 3

DEFAULT_CONFIG = {
    "schedule": "daily",      # off | daily | weekly
    "time": "03:00",
    "weekday": 6,             # for weekly: Monday = 0
    "password": "",           # backup password (encryption)
    "locations": {kind: {"enabled": False} for kind in targets.KINDS},
}


class BackupError(Exception):
    pass


def _scratch() -> Path:
    path = settings.tmp_dir / "backups"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _stage_dir() -> Path:
    path = settings.tmp_dir / "restore"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


# ---------- config ----------

def get_config() -> dict:
    saved = json.loads(db.get_setting(CONFIG_KEY, "{}"))
    saved.pop("encrypt_home", None)   # older versions: every copy is encrypted now
    cfg = {**DEFAULT_CONFIG, **saved}
    cfg["locations"] = {k: {**DEFAULT_CONFIG["locations"][k], **saved.get("locations", {}).get(k, {})}
                        for k in targets.KINDS}
    return cfg


def save_config(update: dict) -> dict:
    """Merge settings from the UI. Empty secret fields keep the saved value."""
    cfg = get_config()
    for key in ("schedule", "time", "weekday"):
        if key in update:
            cfg[key] = update[key]
    if update.get("password"):
        if len(update["password"]) < 8:
            raise BackupError("Use at least 8 characters for the backup password.")
        cfg["password"] = update["password"]
    if update.get("clear_password"):
        cfg["password"] = ""
    for kind, loc in (update.get("locations") or {}).items():
        if kind not in targets.KINDS:
            continue
        current = cfg["locations"][kind]
        for field, value in loc.items():
            if field in targets.SECRET_FIELDS and value in ("", None):
                continue
            if field == "config" and value:
                targets.parse_rclone(value)   # refuse unsafe rclone configs right away
            current[field] = value.strip() if isinstance(value, str) and field != "config" else value
    db.set_setting(CONFIG_KEY, json.dumps(cfg))
    return cfg


def _save_rclone_config(conf: str) -> None:
    """rclone refreshed its login token: keep the new config (stored encrypted)."""
    cfg = get_config()
    cfg["locations"]["rclone"]["config"] = conf
    db.set_setting(CONFIG_KEY, json.dumps(cfg))


def public_config() -> dict:
    """Config for the browser: secrets are never sent back, only whether they're set."""
    cfg = get_config()
    out = {k: v for k, v in cfg.items() if k not in ("password", "locations")}
    out["password_set"] = bool(cfg["password"])
    out["server_key"] = db.encrypted()
    out["locations"] = {}
    for kind, loc in cfg["locations"].items():
        out["locations"][kind] = {f: ("" if f in targets.SECRET_FIELDS else v) for f, v in loc.items()}
        out["locations"][kind].update({f"{f}_set": bool(loc.get(f)) for f in targets.SECRET_FIELDS if f in loc})
    return out


def runs() -> list[dict]:
    return json.loads(db.get_setting(RUNS_KEY, "[]"))


def _record(run: dict) -> None:
    db.set_setting(RUNS_KEY, json.dumps(([run] + runs())[:20]))


# ---------- encoding ----------

def _key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def _server_password() -> str | None:
    """Key for copies kept on this server before a backup password exists."""
    key = db.subkey("local-backups")
    return key.hex() if key else None


def encode(raw: bytes, password: str | None) -> bytes:
    packed = gzip.compress(raw, compresslevel=9)
    if not password:
        return packed
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_key(password, salt)).encrypt(nonce, packed, MAGIC)


def _gunzip(data: bytes) -> bytes:
    """Unpack with a size limit, so a tiny crafted file can't fill the memory."""
    unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = unpacker.decompress(data, MAX_UNPACKED + 1)
    except zlib.error as e:
        raise BackupError("This isn't a DayScore backup, or the file is damaged.") from e
    if len(out) > MAX_UNPACKED or unpacker.unconsumed_tail:
        raise BackupError("This backup unpacks to more than 500 MB, which a DayScore database never is.")
    if not unpacker.eof:
        raise BackupError("This backup file is incomplete or damaged.")
    return out


def decode(data: bytes, passwords: list[str | None] | str | None) -> bytes:
    """Decrypt (trying each password) and unpack a backup file."""
    if isinstance(passwords, str) or passwords is None:
        passwords = [passwords]
    candidates = [p for p in dict.fromkeys(passwords) if p]
    if data.startswith(MAGIC):
        if not candidates:
            raise BackupError("This backup is encrypted. Enter the backup password.")
        body = data[len(MAGIC):]
        salt, nonce, cipher = body[:16], body[16:28], body[28:]
        for password in candidates:
            try:
                data = AESGCM(_key(password, salt)).decrypt(nonce, cipher, MAGIC)
                break
            except InvalidTag:
                continue
        else:
            raise BackupError("Wrong backup password (or the file is damaged).")
    return _gunzip(data)


# ---------- snapshot + inspection ----------

def snapshot() -> tuple[bytes, str]:
    """A consistent plain copy of the live database (safe while DayScore runs), plus a
    fingerprint of its content that ignores the backup system's own bookkeeping."""
    path = _scratch() / f"snapshot-{secrets.token_hex(6)}.db"
    try:
        db.export_plain(path)
        conn = sqlite3.connect(path)
        try:
            digest = hashlib.sha256()
            for line in conn.iterdump():
                if not line.startswith("INSERT INTO \"settings\" VALUES('backup."):
                    digest.update(line.encode())
        finally:
            conn.close()
        return path.read_bytes(), digest.hexdigest()
    finally:
        path.unlink(missing_ok=True)


def _inspect_file(path: Path) -> dict:
    try:
        conn = sqlite3.connect(path)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("The backup's database is damaged.")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > db.SCHEMA_VERSION:
                raise BackupError("This backup is from a newer DayScore version. Update DayScore first, then restore it.")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"entries", "settings"} <= tables:
                raise BackupError("This file isn't a DayScore database.")
            days, first, last = conn.execute("SELECT COUNT(*), MIN(day), MAX(day) FROM entries").fetchone()
            keys = {r[0] for r in conn.execute("SELECT key FROM settings")}
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise BackupError("This file isn't a DayScore database.") from e
    return {"days": days, "first_day": first, "last_day": last, "version": version,
            "has_password": "auth.password_hash" in keys, "has_2fa": "auth.totp_secret" in keys}


def inspect(raw: bytes) -> dict:
    """Check that the bytes are a healthy DayScore database and describe what's inside."""
    path = _scratch() / f"check-{secrets.token_hex(6)}.db"
    try:
        _write_private(path, raw)
        return _inspect_file(path)
    finally:
        path.unlink(missing_ok=True)


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


# ---------- keeping old backups (7 daily, 4 weekly, 6 monthly) ----------

def _stamp(name: str) -> datetime | None:
    m = NAME_RE.match(name)
    return datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y-%m-%d_%H%M%S") if m else None


def to_prune(names: list[str]) -> list[str]:
    dated = sorted(((s, n) for n in names if (s := _stamp(n))), reverse=True)
    keep: set[str] = set(n for _, n in dated[:1])  # always the newest
    for bucket, count in (("daily", KEEP["daily"]), ("weekly", KEEP["weekly"]), ("monthly", KEEP["monthly"])):
        seen: list[str] = []
        for stamp, name in dated:
            key = {"daily": stamp.strftime("%Y-%m-%d"), "weekly": "%d-W%02d" % stamp.isocalendar()[:2],
                   "monthly": stamp.strftime("%Y-%m")}[bucket]
            if key not in seen:
                seen.append(key)
                if len(seen) > count:
                    break
                keep.add(name)
    return [n for _, n in dated if n not in keep]


# ---------- running a backup ----------

def _enabled_targets(cfg: dict) -> list[tuple[str, dict]]:
    out = [("local", {})]
    out += [(kind, loc) for kind, loc in cfg["locations"].items() if loc.get("enabled")]
    return out


def _password_for(kind: str, cfg: dict) -> str | None:
    if cfg["password"]:
        return cfg["password"]
    if kind == "local":
        return _server_password()
    raise BackupError("Set a backup password first: every copy outside this server is encrypted with it.")


def _hash_key(kind: str) -> str:
    return f"backup.hash.{kind}"


async def run_backup(trigger: str = "manual") -> dict:
    """trigger: manual (every location), schedule or retry (only locations that don't have the
    current data yet)."""
    cfg = get_config()
    raw, digest = await asyncio.to_thread(snapshot)
    now = datetime.now(prefs.tz())
    run = {"at": now.isoformat(timespec="seconds"), "trigger": trigger, "results": []}
    todo = _enabled_targets(cfg)
    if trigger != "manual":
        todo = [(k, loc) for k, loc in todo if db.get_setting(_hash_key(k)) != digest]
        if not todo:
            run["skipped"] = True   # every location already has this data
            if trigger == "schedule":
                _record(run)
            db.delete_setting(RETRY_KEY)
            return run

    base = f"dayscore-{now:%Y-%m-%d_%H%M%S}.db.gz"
    for kind, loc in todo:
        result = {"location": kind}
        try:
            password = _password_for(kind, cfg)
            data = await asyncio.to_thread(encode, raw, password)
            name = base + (".enc" if password else "")
            target = targets.make(kind, loc, _save_rclone_config)
            await asyncio.to_thread(target.put, name, data)
            existing = [i["name"] for i in await asyncio.to_thread(target.list)]
            for old in to_prune(existing):
                await asyncio.to_thread(target.delete, old)
            db.set_setting(_hash_key(kind), digest)
            result.update(ok=True, name=name, size=len(data))
        except (BackupError, targets.TargetError) as e:
            result.update(ok=False, error=str(e))
        except Exception as e:
            log.exception("Backup to %s failed", kind)
            result.update(ok=False, error=f"Unexpected error: {e}")
        run["results"].append(result)
    _record(run)
    failed = [r for r in run["results"] if not r["ok"]]
    log.info("Backup (%s): %d ok, %d failed", trigger, len(run["results"]) - len(failed), len(failed))
    await _after_run(trigger, failed)
    return run


async def _after_run(trigger: str, failed: list[dict]) -> None:
    """Retry failed locations later (up to 3 times), and tell the owner on Telegram."""
    if not failed:
        db.delete_setting(RETRY_KEY)
        return
    state = json.loads(db.get_setting(RETRY_KEY, "{}")) if trigger == "retry" else {}
    count = state.get("count", 0) + (1 if trigger == "retry" else 0)
    if count < MAX_RETRIES and trigger != "manual":
        db.set_setting(RETRY_KEY, json.dumps({"at": time.time() + RETRY_AFTER, "count": count}))
    else:
        db.delete_setting(RETRY_KEY)
    if trigger == "retry" and count < MAX_RETRIES:
        return   # only alert on the first failure and when giving up
    names = {"local": "This server", **{k: v["label"] for k, v in targets.KINDS.items()}}
    lines = "\n".join(f"• {names.get(r['location'], r['location'])}: {r['error']}" for r in failed)
    later = " I'll try again in an hour." if trigger == "schedule" else ""
    from . import telegram   # imported here: telegram imports service, which is heavier
    await telegram.notify(f"⚠️ <b>Backup problem</b>\n{telegram.escape(lines)}\n\nCheck Settings → Backups.{later}")


def due(cfg: dict, now: datetime) -> bool:
    if cfg["schedule"] == "off":
        return False
    hh, mm = (int(x) for x in cfg["time"].split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return False
    if cfg["schedule"] == "weekly" and now.weekday() != int(cfg["weekday"]):
        return False
    return db.get_setting("backup.last_scheduled") != now.date().isoformat()


def retry_due() -> bool:
    state = json.loads(db.get_setting(RETRY_KEY, "{}"))
    return bool(state) and time.time() >= state.get("at", 0)


async def scheduler() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(prefs.tz())
            if due(get_config(), now):
                db.set_setting("backup.last_scheduled", now.date().isoformat())
                await run_backup("schedule")
            elif retry_due():
                await run_backup("retry")
            _clean_stage()
        except Exception:
            log.exception("Scheduled backup failed")


# ---------- start-up housekeeping ----------

def secure_local_backups() -> int:
    """Encrypt plain copies left on this server by older versions (when a key is available)."""
    cfg = get_config()
    password = cfg["password"] or _server_password()
    if not password:
        return 0
    local = targets.local_target()
    count = 0
    for item in local.list():
        name = item["name"]
        if name.endswith(".enc") or not (NAME_RE.match(name) or PRE_RESTORE_RE.match(name)):
            continue
        try:
            raw = decode(local.get(name), None)
            local.put(name + ".enc", encode(raw, password))
            local.delete(name)
            count += 1
        except (BackupError, targets.TargetError) as e:
            log.warning("Couldn't encrypt the old backup %s: %s", name, e)
    if count:
        log.warning("Encrypted %d older backup(s) on this server.", count)
    return count


def startup() -> None:
    targets.remove_legacy_files()
    legacy_stage = settings.data_dir / "restore"   # older versions staged restores on the volume
    if legacy_stage.is_dir():
        for f in legacy_stage.glob("*.db"):
            f.unlink(missing_ok=True)
    secure_local_backups()


# ---------- listing / downloading ----------

def _valid(name: str) -> bool:
    return bool(NAME_RE.match(name) or PRE_RESTORE_RE.match(name))


def _target(kind: str):
    cfg = get_config()
    if kind != "local" and kind not in targets.KINDS:
        raise BackupError("Unknown backup location.")
    return targets.make(kind, {} if kind == "local" else cfg["locations"].get(kind, {}), _save_rclone_config)


async def list_backups(kind: str) -> list[dict]:
    items = await asyncio.to_thread(_target(kind).list)
    items = [{**i, "encrypted": i["name"].endswith(".enc"), "safety_copy": bool(PRE_RESTORE_RE.match(i["name"]))}
             for i in items if _valid(i["name"])]
    return sorted(items, key=lambda i: i["name"].replace("pre-restore-", ""), reverse=True)


async def fetch(kind: str, name: str) -> bytes:
    if not _valid(name):
        raise BackupError("Unknown backup file.")
    return await asyncio.to_thread(_target(kind).get, name)


# ---------- restore: stage (check + preview), then commit ----------

def _clean_stage() -> None:
    folder = settings.tmp_dir / "restore"
    if folder.is_dir():
        for f in folder.glob("*.db"):
            if f.stat().st_mtime < time.time() - STAGE_TTL:
                f.unlink(missing_ok=True)


def stage(data: bytes, password: str | None) -> dict:
    """Decrypt, unpack and verify a backup, keep it aside (in RAM-only scratch), and describe it."""
    raw = decode(data, [password, get_config()["password"], _server_password()])
    info = inspect(raw)
    token = secrets.token_urlsafe(16)
    _write_private(_stage_dir() / f"{token}.db", raw)
    return {"token": token, **info}


async def commit(token: str) -> dict:
    """Replace the live database with a staged backup, after a safety copy of the current one."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", token or ""):
        raise BackupError("This restore expired. Please start again.")
    staged = _stage_dir() / f"{token}.db"
    if not staged.exists():
        raise BackupError("This restore expired. Please start again.")
    info = await asyncio.to_thread(_inspect_file, staged)   # check once more right before swapping

    # Safety copy of the current state, so a restore can be undone.
    safety = None
    if settings.db_path.exists():
        raw, _ = await asyncio.to_thread(snapshot)
        password = _password_for("local", get_config())
        safety = f"dayscore-pre-restore-{datetime.now(prefs.tz()):%Y-%m-%d_%H%M%S}.db.gz" + (".enc" if password else "")
        local = targets.local_target()
        data = await asyncio.to_thread(encode, raw, password)
        await asyncio.to_thread(local.put, safety, data)
        old = sorted(i["name"] for i in local.list() if PRE_RESTORE_RE.match(i["name"]))[:-3]
        for name in old:   # keep the last 3 safety copies
            local.delete(name)

    try:
        await asyncio.to_thread(db.replace_with, staged)   # encrypts it and upgrades the schema
    finally:
        staged.unlink(missing_ok=True)
    for kind in ["local", *targets.KINDS]:
        db.delete_setting(_hash_key(kind))   # the next scheduled backup goes everywhere again
    log.warning("Database restored from a backup (%s days); safety copy: %s", info["days"], safety)
    return {**info, "safety_copy": safety}

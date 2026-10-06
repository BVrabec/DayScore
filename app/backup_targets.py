"""Backup locations. Every location offers the same five actions, all synchronous
(run them with asyncio.to_thread): test(), put(name, data), list(), get(name), delete(name).

- local   : /data/backups inside the container (always on)
- folder  : any folder mapped into the container (NAS mount, USB disk...)
- smb     : a Windows/NAS share, directly (smbprotocol)
- webdav  : Nextcloud, ownCloud or any WebDAV server
- s3      : any S3-compatible storage (Backblaze B2, Cloudflare R2, AWS S3, Wasabi, MinIO)
- rclone  : Google Drive, Dropbox or OneDrive through an rclone remote
"""

import configparser
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree

import httpx2 as httpx

from .config import settings


class TargetError(Exception):
    pass


def check_host(host: str) -> None:
    """Refuse addresses that point back at this server or at a cloud metadata service, so a
    backup location can't be used to probe them. Private LAN addresses (a NAS) are fine."""
    if os.environ.get("BACKUP_ALLOW_LOOPBACK") == "1":   # only for tests
        return
    host = host.strip().strip("[]")
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError) as e:
        raise TargetError(f"Can't find the server {host}. Check the address.") from e
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast:
            raise TargetError(f"{host} points to this server or a reserved address, which isn't allowed.")


def _entry(name: str, size: int, modified: datetime | float) -> dict:
    if isinstance(modified, (int, float)):
        modified = datetime.fromtimestamp(modified, timezone.utc)
    return {"name": name, "size": size, "modified": modified.astimezone(timezone.utc).isoformat(timespec="seconds")}


# ---------- local and mounted folders ----------

class FolderTarget:
    def __init__(self, path: str):
        if not path:
            raise TargetError("Set a folder path.")
        self.dir = Path(path)

    def test(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            probe = self.dir / ".dayscore-test"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as e:
            raise TargetError(f"Can't write to {self.dir}: {e.strerror or e}") from e

    def _io(self, fn, what: str):
        try:
            return fn()
        except OSError as e:
            raise TargetError(f"{what} in {self.dir} failed: {e.strerror or e}") from e

    def put(self, name: str, data: bytes) -> None:
        def write():
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f".{name}.part"
            tmp.write_bytes(data)
            os.chmod(tmp, 0o600)
            tmp.replace(self.dir / name)   # never leave a half-written backup behind
        self._io(write, "Saving")

    def list(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        return self._io(lambda: [_entry(p.name, p.stat().st_size, p.stat().st_mtime) for p in self.dir.iterdir()
                                 if p.is_file() and not p.name.startswith(".")], "Listing")

    def get(self, name: str) -> bytes:
        return self._io(lambda: (self.dir / name).read_bytes(), "Reading")

    def delete(self, name: str) -> None:
        self._io(lambda: (self.dir / name).unlink(missing_ok=True), "Deleting")


def local_target() -> FolderTarget:
    return FolderTarget(str(settings.data_dir / "backups"))


# ---------- SMB share ----------

class SMBTarget:
    def __init__(self, cfg: dict):
        import smbclient  # imported lazily: only needed when SMB is used
        self.smb = smbclient
        self.server = (cfg.get("server") or "").strip().strip("\\/")
        self.share = (cfg.get("share") or "").strip().strip("\\/")
        if not self.server or not self.share:
            raise TargetError("Set the server and the share name.")
        folder = (cfg.get("folder") or "").strip().strip("\\/").replace("/", "\\")
        self.dir = f"\\\\{self.server}\\{self.share}" + (f"\\{folder}" if folder else "")
        self.username = cfg.get("username") or None
        self.password = cfg.get("password") or None

    def _connect(self) -> None:
        check_host(self.server)
        try:
            self.smb.register_session(self.server, username=self.username, password=self.password,
                                      connection_timeout=15)
        except Exception as e:  # smbprotocol raises many different types
            if "logon" in str(e).lower() or "password" in str(e).lower():
                raise TargetError("The server didn't accept the username or password.") from e
            raise TargetError(f"Couldn't connect to {self.server}: {e}") from e

    def test(self) -> None:
        self.smb.reset_connection_cache()
        self._connect()
        try:
            self.smb.makedirs(self.dir, exist_ok=True)
            probe = f"{self.dir}\\.dayscore-test"
            with self.smb.open_file(probe, mode="wb") as f:
                f.write(b"ok")
            self.smb.remove(probe)
        except Exception as e:
            raise TargetError(f"Can't write to {self.dir}: {e}") from e

    def put(self, name: str, data: bytes) -> None:
        self._connect()
        self.smb.makedirs(self.dir, exist_ok=True)
        with self.smb.open_file(f"{self.dir}\\{name}", mode="wb") as f:
            f.write(data)

    def list(self) -> list[dict]:
        self._connect()
        try:
            entries = list(self.smb.scandir(self.dir))
        except Exception:
            return []
        out = []
        for e in entries:
            if e.is_file() and not e.name.startswith("."):
                st = e.stat()
                out.append(_entry(e.name, st.st_size, st.st_mtime))
        return out

    def get(self, name: str) -> bytes:
        self._connect()
        with self.smb.open_file(f"{self.dir}\\{name}", mode="rb") as f:
            return f.read()

    def delete(self, name: str) -> None:
        self._connect()
        self.smb.remove(f"{self.dir}\\{name}")


# ---------- WebDAV / Nextcloud ----------

class WebDAVTarget:
    def __init__(self, cfg: dict):
        url = (cfg.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise TargetError("Set the WebDAV address, starting with https://")
        self.url = url.rstrip("/") + "/"
        self.auth = (cfg.get("username") or "", cfg.get("password") or "")

    def _client(self) -> httpx.Client:
        check_host(urlparse(self.url).hostname or "")
        return httpx.Client(auth=self.auth, timeout=60.0, follow_redirects=False)

    def _check(self, resp: httpx.Response, what: str) -> None:
        if 300 <= resp.status_code < 400:
            where = resp.headers.get("location", "")
            raise TargetError(f"The server sent DayScore to another address{f' ({where})' if where else ''}. "
                              "Enter that address instead.")
        if resp.status_code in (401, 403):
            raise TargetError("The server didn't accept the username or password.")
        if resp.status_code >= 400:
            raise TargetError(f"{what} failed ({resp.status_code}).")

    def _ensure_folder(self, client: httpx.Client) -> None:
        resp = client.request("MKCOL", self.url)
        if resp.status_code not in (201, 405):   # 405 = already exists
            self._check(resp, "Creating the folder")

    def test(self) -> None:
        try:
            with self._client() as c:
                self._ensure_folder(c)
                self._check(c.put(self.url + ".dayscore-test", content=b"ok"), "Writing a test file")
                c.delete(self.url + ".dayscore-test")
        except httpx.HTTPError as e:
            raise TargetError(f"Couldn't reach the WebDAV server: {e}") from e

    def put(self, name: str, data: bytes) -> None:
        with self._client() as c:
            self._ensure_folder(c)
            self._check(c.put(self.url + quote(name), content=data), "Upload")

    def list(self) -> list[dict]:
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                '<d:getcontentlength/><d:getlastmodified/><d:resourcetype/></d:prop></d:propfind>')
        with self._client() as c:
            resp = c.request("PROPFIND", self.url, content=body, headers={"Depth": "1", "Content-Type": "application/xml"})
        if resp.status_code == 404:
            return []
        self._check(resp, "Listing")
        ns = {"d": "DAV:"}
        folder = unquote(urlparse(self.url).path).rstrip("/")
        out = []
        for r in ElementTree.fromstring(resp.content).findall("d:response", ns):
            href = unquote(urlparse(r.findtext("d:href", "", ns)).path).rstrip("/")
            name = href.rsplit("/", 1)[-1]
            props = r.findall("d:propstat/d:prop", ns)   # servers may split props over several propstat blocks
            if href == folder or not name or name.startswith(".") \
                    or any(p.find("d:resourcetype/d:collection", ns) is not None for p in props):
                continue
            size = next((p.findtext("d:getcontentlength", None, ns) for p in props
                         if p.findtext("d:getcontentlength", None, ns)), "0")
            modified = next((p.findtext("d:getlastmodified", None, ns) for p in props
                             if p.findtext("d:getlastmodified", None, ns)), "")
            try:
                when = datetime.strptime(modified, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            except ValueError:
                when = datetime.now(timezone.utc)
            out.append(_entry(name, int(size or 0), when))
        return out

    def get(self, name: str) -> bytes:
        with self._client() as c:
            resp = c.get(self.url + quote(name))
        self._check(resp, "Download")
        return resp.content

    def delete(self, name: str) -> None:
        with self._client() as c:
            c.delete(self.url + quote(name))


# ---------- S3-compatible ----------

class S3Target:
    def __init__(self, cfg: dict):
        from minio import Minio  # imported lazily
        endpoint = (cfg.get("endpoint") or "").strip()
        self.bucket = (cfg.get("bucket") or "").strip()
        if not endpoint or not self.bucket:
            raise TargetError("Set the endpoint and the bucket.")
        parsed = urlparse(endpoint if "://" in endpoint else "https://" + endpoint)
        check_host(parsed.hostname or "")
        self.prefix = (cfg.get("prefix") or "").strip().strip("/")
        self.prefix = self.prefix + "/" if self.prefix else ""
        self.client = Minio(parsed.netloc, access_key=cfg.get("access_key") or None,
                            secret_key=cfg.get("secret_key") or None,
                            secure=parsed.scheme != "http", region=(cfg.get("region") or None))

    def _wrap(self, fn, what: str):
        try:
            return fn()
        except Exception as e:  # minio raises S3Error, urllib3 errors...
            raise TargetError(f"{what} failed: {getattr(e, 'message', None) or e}") from e

    def test(self) -> None:
        if not self._wrap(lambda: self.client.bucket_exists(self.bucket), "Connecting"):
            raise TargetError(f"The bucket '{self.bucket}' doesn't exist.")
        self.put(".dayscore-test", b"ok")
        self.delete(".dayscore-test")

    def put(self, name: str, data: bytes) -> None:
        self._wrap(lambda: self.client.put_object(self.bucket, self.prefix + name, io.BytesIO(data), len(data)), "Upload")

    def list(self) -> list[dict]:
        objs = self._wrap(lambda: list(self.client.list_objects(self.bucket, prefix=self.prefix, recursive=False)), "Listing")
        out = []
        for o in objs:
            name = o.object_name[len(self.prefix):]
            if name and "/" not in name and not name.startswith("."):
                out.append(_entry(name, o.size or 0, o.last_modified or datetime.now(timezone.utc)))
        return out

    def get(self, name: str) -> bytes:
        def fetch():
            resp = self.client.get_object(self.bucket, self.prefix + name)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()
        return self._wrap(fetch, "Download")

    def delete(self, name: str) -> None:
        self._wrap(lambda: self.client.remove_object(self.bucket, self.prefix + name), "Deleting")


# ---------- rclone (Google Drive, Dropbox, OneDrive) ----------

RCLONE_TYPES = {"drive": "Google Drive", "dropbox": "Dropbox", "onedrive": "OneDrive"}
# Only the settings these remotes normally have. Anything else is refused: some rclone options
# run programs or read files on the server.
RCLONE_KEYS = {"type", "client_id", "client_secret", "scope", "token", "team_drive", "root_folder_id",
               "drive_id", "drive_type", "region", "tenant", "access_scopes", "description",
               "service_account_credentials", "shared_with_me", "chunk_size", "upload_cutoff"}


def parse_rclone(conf: str) -> tuple[str, str]:
    """Validate a pasted rclone config. Returns (remote name, normalised config text)."""
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(conf)
    except configparser.Error as e:
        raise TargetError("That isn't a valid rclone config. Paste what `rclone config show NAME` prints.") from e
    sections = parser.sections()
    if len(sections) != 1:
        raise TargetError("Paste exactly one rclone remote, starting with a line like [gdrive].")
    name = sections[0].strip()
    if not re.fullmatch(r"[\w.-]+", name):
        raise TargetError("Use a remote name with only letters, digits, dots, dashes or underscores.")
    section = parser[name]
    kind = section.get("type", "").strip()
    if kind not in RCLONE_TYPES:
        raise TargetError("Only Google Drive, Dropbox and OneDrive remotes are supported here.")
    for key in section:
        if key not in RCLONE_KEYS:
            raise TargetError(f"The rclone setting '{key}' isn't allowed here, for safety. "
                              "Remove that line and try again.")
    lines = [f"[{name}]"] + [f"{k} = {v}" for k, v in section.items()]
    return name, "\n".join(lines) + "\n"


class RcloneTarget:
    """The config (with its login token) only ever exists as a file in RAM-only scratch space
    while rclone runs. When rclone refreshes the token, the new config is handed to on_change
    to be saved (encrypted) with the other settings."""

    def __init__(self, cfg: dict, on_change=None):
        self.remote, self.conf = parse_rclone(cfg.get("config") or "")
        folder = (cfg.get("folder") or "DayScore").strip().strip("/")
        if not re.fullmatch(r"[\w .()/-]+", folder) or ".." in folder.split("/"):
            raise TargetError("Use a simple folder name, like DayScore.")
        self.base = f"{self.remote}:{folder}"
        self.on_change = on_change

    def _run(self, *args: str, data: bytes | None = None) -> bytes:
        work = settings.tmp_dir / "rclone"
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        conf_file = work / f"{os.getpid()}-{id(self)}.conf"
        fd = os.open(conf_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.conf)
        # A clean environment: no RCLONE_* variables from outside can change what it does.
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(work)}
        try:
            proc = subprocess.run(["rclone", "--config", str(conf_file), "--ask-password=false", *args],
                                  input=data, capture_output=True, timeout=180, env=env)
            updated = conf_file.read_text()
        except FileNotFoundError as e:
            raise TargetError("rclone isn't installed in this container.") from e
        except subprocess.TimeoutExpired as e:
            raise TargetError("rclone took too long.") from e
        finally:
            conf_file.unlink(missing_ok=True)
        if updated.strip() != self.conf.strip():
            try:
                _, self.conf = parse_rclone(updated)   # e.g. a refreshed login token
                if self.on_change:
                    self.on_change(self.conf)
            except TargetError:
                pass
        if proc.returncode != 0:
            msg = proc.stderr.decode(errors="replace").strip().splitlines()
            raise TargetError(f"rclone: {msg[-1] if msg else 'failed'}")
        return proc.stdout

    def test(self) -> None:
        self._run("mkdir", self.base)
        self.put(".dayscore-test", b"ok")
        self.delete(".dayscore-test")

    def put(self, name: str, data: bytes) -> None:
        self._run("rcat", f"{self.base}/{name}", data=data)

    def list(self) -> list[dict]:
        try:
            items = json.loads(self._run("lsjson", "--files-only", self.base) or b"[]")
        except TargetError as e:
            if "directory not found" in str(e).lower():
                return []
            raise
        return [_entry(i["Name"], i.get("Size", 0), datetime.fromisoformat(i["ModTime"].replace("Z", "+00:00")))
                for i in items if not i["Name"].startswith(".")]

    def get(self, name: str) -> bytes:
        return self._run("cat", f"{self.base}/{name}")

    def delete(self, name: str) -> None:
        self._run("deletefile", f"{self.base}/{name}")


def remove_legacy_files() -> None:
    """Older versions kept rclone's config (with its token) in plain text on the data volume."""
    for name in ("rclone.conf", ".rclone-config.sha256"):
        (settings.data_dir / name).unlink(missing_ok=True)


# ---------- registry ----------

KINDS = {
    "folder": {"label": "Folder / NAS mount", "offsite": False},
    "smb": {"label": "SMB share", "offsite": False},
    "webdav": {"label": "WebDAV / Nextcloud", "offsite": True},
    "s3": {"label": "S3-compatible", "offsite": True},
    "rclone": {"label": "Google Drive / Dropbox / OneDrive", "offsite": True},
}
SECRET_FIELDS = {"password", "secret_key", "config"}


def make(kind: str, cfg: dict, on_change=None):
    if kind == "local":
        return local_target()
    if kind == "folder":
        return FolderTarget((cfg.get("path") or "").strip())
    if kind == "smb":
        return SMBTarget(cfg)
    if kind == "webdav":
        return WebDAVTarget(cfg)
    if kind == "s3":
        return S3Target(cfg)
    if kind == "rclone":
        return RcloneTarget(cfg, on_change)
    raise TargetError(f"Unknown backup location: {kind}")

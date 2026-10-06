# Changelog

All notable changes. Versions follow [semantic versioning](https://semver.org).

## [1.22.0] - 2026-10-06

This release encrypts your data. **Before updating, create `DAYSCORE_KEY`** (see "Updating
from a version before 1.22" in the README). Existing data and backups are encrypted on the
first start.

### Added
- **Encryption at rest**: the database is encrypted with SQLCipher (AES-256) using
  `DAYSCORE_KEY`; a copy of the data volume alone reveals nothing.
- **Backups** to a folder/NAS, SMB, WebDAV/Nextcloud, S3-compatible storage, and Google Drive,
  Dropbox or OneDrive (rclone): compressed, always encrypted, on a schedule, with 7 daily /
  4 weekly / 6 monthly copies kept. Failed locations are retried and reported on Telegram.
- **Restore** from Settings (preview, account password, safety copy) or onto a fresh install.
- **Your own AI server**: Ollama, LM Studio or any OpenAI-compatible API.
- **Edit or delete a day**, an optional **mood** per day, and a **weekly summary** on Telegram.
- Every day shows **why** it got its score and a tip; the chart marks AI model changes.
- **Sign out everywhere**, and `scripts/reset_password.py` for a forgotten password.
- A one-time **setup code** (in the server logs) is needed to claim a new install.
- Published Docker images for amd64 and arm64 (`ghcr.io/bvrabec/dayscore`), tests and CI.

### Changed
- Changing the AI provider, Telegram bot, Todoist token or backup locations asks for your
  password again (valid for 10 minutes).
- Scores use a fixed scale instead of following your recent average.
- OpenRouter is asked to use only providers that don't store or train on prompts.
- The Telegram bot keeps retrying when the network or Telegram is down, slows down on errors,
  and shows its status in Settings. A failed scoring offers a "Try again" button.
- The container runs read-only, without capabilities, with a RAM-only `/tmp`.
- Dependencies are pinned with hashes; rclone is a pinned, checksum-verified release.

### Fixed
- Two notes for the same day arriving together could lose one of them.
- The backup password could end up in access logs.
- A crafted backup file could use up all memory during a restore.

### Removed
- `scripts/backup.py` and `scripts/restore.py`: use Settings → Backups instead.

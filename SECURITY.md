# Security

## Reporting a problem

Please **don't open a public issue** for security problems. Use GitHub's private reporting:
[Report a vulnerability](https://github.com/BVrabec/DayScore/security/advisories/new).
Describe what you found and how to reproduce it. You'll get an answer within a week.

DayScore is a personal, volunteer project with no bug bounty. Fixes go into the next release,
listed in [CHANGELOG.md](CHANGELOG.md).

## How DayScore protects your data

- **At rest:** the database is encrypted with SQLCipher (AES-256) using `DAYSCORE_KEY`, which
  lives outside the data volume. Backups are always encrypted (AES-256-GCM, key derived from a
  password with scrypt). Decrypted copies (while a backup is made or checked) only exist in a
  RAM-only `tmpfs`.
- **Sign-in:** PBKDF2 password hashing (600,000 rounds), optional TOTP two-factor codes with
  hashed recovery codes, lockout after 5 failed attempts, a one-time setup code for new
  installs, and `SameSite=Strict`, `HttpOnly` cookies.
- **Sensitive changes** (AI provider, Telegram bot, Todoist, backup locations) need the
  password again. Changing the password or turning on two-factor signs out other devices.
- **Backup locations** can't point at the server itself or cloud metadata addresses, and
  rclone only accepts Google Drive, Dropbox and OneDrive configs with known-safe settings.
- **Web:** a strict Content-Security-Policy (no inline scripts), all output escaped, and CSV
  exports protected against spreadsheet formulas.
- **Container:** runs as a non-root user with a read-only filesystem, no Linux capabilities
  and `no-new-privileges`. Dependencies are pinned with hashes and checked for known
  vulnerabilities in CI.

## What it can't protect against

- Anyone who can log in to the host as root or run Docker commands can read the key and the data.
- Notes are sent to the AI provider you choose (unless you use your own AI server). See
  [What leaves your server](README.md#what-leaves-your-server).

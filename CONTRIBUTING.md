# Contributing to DayScore

Thanks for your interest! Every kind of help is welcome: ideas, bug reports, docs and code.

## Ideas and questions

[Start a discussion](https://github.com/BVrabec/DayScore/discussions). Describe what you'd
like to do and why; small, concrete ideas are the easiest to pick up. The current plans are
in the [Roadmap](README.md#roadmap).

## Bugs

[Open an issue](https://github.com/BVrabec/DayScore/issues/new/choose) with:
- what you did, what you expected, and what happened instead
- how you run DayScore (LXC, VM, Raspberry Pi…) and your version (`git log -1 --oneline`)
- the relevant lines from `docker compose logs`

Please remove API keys, tokens and anything personal from logs before posting.

## Code

1. Fork the repo and create a branch.
2. Run it locally without Docker:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload --port 8000
   ```
   To get realistic sample data, run `DATA_DIR=./demo-data .venv/bin/python -m scripts.seed_demo`
   and then start it with `DATA_DIR=./demo-data` (password: `demo`).
3. Keep changes focused. Match the existing style: plain Python, vanilla JavaScript,
   no new frameworks without a good reason.
4. Open a pull request describing what changed and how you tested it.

## Project layout

```
app/
  main.py       web server and API
  scoring.py    AI prompt and providers (Anthropic, OpenRouter)
  service.py    logging a day, stats
  telegram.py   Telegram bot and reminders
  todoist.py    Todoist integration
  security.py   2FA (authenticator app), recovery codes, lockout
  db.py         SQLite storage
  static/       the web interface (HTML, CSS, JS)
scripts/        demo data, backup, restore, 2FA reset
```

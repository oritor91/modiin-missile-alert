# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot
python main.py

# Run with a specific log level
LOG_LEVEL=DEBUG python main.py
```

No test suite or linter is configured. There is no build step.

## Architecture

This is a single-file Python bot (`main.py`) with three concurrent concerns:

1. **Telegram bot** — uses `python-telegram-bot` v21 (async API). Handles `/start` and `/city` commands, inline keyboard callbacks, and free-text city input. Bot handlers and the poll loop run in the same asyncio event loop.

2. **oref.org.il polling** (`poll_loop`) — an infinite async loop that hits `alerts.json` every `POLL_INTERVAL` seconds. Alert deduplication is done in-memory via `_sent_alerts` (a dict keyed by chat_id, storing the last alert id sent). When the API returns empty/null (no active alert), all dedup state is cleared so the next real alert fires again.

3. **User persistence** — `users.json` maps `str(chat_id)` → Hebrew city name. Loaded and saved on every interaction (not cached between events). `WAITING_FOR_CITY` is an in-memory set tracking users who clicked "Other" and are expected to type a city name next.

## Key details

- The oref API requires `Referer` and `X-Requested-With` headers to avoid HTTP 403. These are in `OREF_HEADERS`.
- Only alerts with `cat=1` (missile/rocket) are acted on — see `get_alerted_cities`.
- Alert text from the API is Hebrew; the bot sends bilingual (Hebrew + English) messages to users.
- `users.json` is gitignored and lives alongside `main.py`.
- Environment is loaded via `python-dotenv` from `.env` (copy from `.env.example`).

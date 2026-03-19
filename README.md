# modiin-missile-alert

A Telegram bot that monitors the IDF Home Front Command (פיקוד העורף) for active missile/rocket alerts and notifies users for their chosen city.

## How It Works

- Polls `oref.org.il` every 2 seconds for active alerts
- On first use, asks each user to choose their city via an inline keyboard
- Sends a notification only when an alert targets the user's registered city
- Deduplicates repeated API responses so each alert fires only once

## Prerequisites

- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd modiin-missile-alert

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN

# 4. Run
python main.py
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `POLL_INTERVAL` | No | `2` | Seconds between API polls |
| `LOG_LEVEL` | No | `INFO` | Logging level |

## Usage

1. Start a chat with your bot on Telegram
2. Send `/start` — the bot will show a city selection keyboard
3. Tap your city (or choose "Other" and type a Hebrew city name)
4. You'll receive a message whenever an active missile alert targets your city
5. Send `/city` at any time to change your registered city

## Notes

- The oref.org.il API may return HTTP 403 from outside Israel without the correct request headers. The bot includes the required `Referer` and `X-Requested-With` headers, which works from most locations.
- User preferences are saved to `users.json` (gitignored) in the project directory.
- This project is not affiliated with the IDF or the Israeli government.

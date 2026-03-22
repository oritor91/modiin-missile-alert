"""
Missile alert Telegram bot.

Polls the oref.org.il REST API for active rocket/missile alerts and notifies
registered users when their chosen area is targeted.

Users register via /start or /city: first pick a city group, then pick the
specific area within that city (matching the exact strings the oref API uses).
"""

import asyncio
import json
import logging
import os
import sys
import unicodedata
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "2"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OREF_API_URL = "https://www.oref.org.il/WarningMessages/alert/alerts.json"
OREF_HEADERS = {
    "Referer": "https://www.oref.org.il/",
    "X-Requested-With": "XMLHttpRequest",
}

# Two-level city → area mapping.
# Keys are displayed on the first keyboard; values are the exact area strings
# that oref.org.il uses in its alert payload. Cities with a single entry skip
# the second keyboard and register directly.
CITY_AREAS: dict[str, list[str]] = {
    "מודיעין": [
        "מודיעין מכבים רעות",
        "מודיעין - ישפרו סנטר",
        "מודיעין - ליגד סנטר",
    ],
    "תל אביב": [
        "תל אביב - עבר הירקון",
        "תל אביב - מרכז העיר",
        "תל אביב - מזרח",
        "תל אביב - דרום העיר ויפו",
    ],
    "ירושלים": ["ירושלים"],
    "חיפה": ["חיפה"],
    "באר שבע": ["באר שבע"],
    "ראשון לציון": [
        "ראשון לציון - מזרח",
        "ראשון לציון - מערב",
    ],
    "רמת גן": [
        "רמת גן - מזרח",
        "רמת גן - מערב",
    ],
    "אשדוד": [
        "אשדוד",
        "אשדוד - איזור תעשייה צפוני",
    ],
    "קריית שמונה": ["קריית שמונה"],
    "נהריה": ["נהריה"],
}

USERS_FILE = Path(__file__).parent / "users.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# User storage  {str(chat_id): city_name}
# ---------------------------------------------------------------------------


def load_users() -> dict[str, str]:
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read users.json, starting fresh.")
    return {}


def save_users(users: dict[str, str]) -> None:
    try:
        USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.error("Failed to save users.json: %s", e)


# ---------------------------------------------------------------------------
# City selection helpers
# ---------------------------------------------------------------------------

WAITING_FOR_CITY: set[int] = set()  # chat_ids that sent "Other"


def normalize(text: str) -> str:
    """NFC-normalize and strip so city comparisons are encoding-agnostic."""
    return unicodedata.normalize("NFC", text).strip()


def city_matches_alert(user_location: str, alerted_cities: set[str]) -> bool:
    """Return True if the user's registered location should receive this alert."""
    norm = normalize(user_location)
    for alerted in alerted_cities:
        if alerted == norm:
            return True
        # Whole-city registration: match "CityName - <zone>"
        if alerted.startswith(norm + " - "):
            return True
    return False


def city_keyboard() -> InlineKeyboardMarkup:
    """First-level keyboard: choose a city group."""
    buttons = [
        [InlineKeyboardButton(city, callback_data=f"citygroup:{city}")]
        for city in CITY_AREAS
    ]
    buttons.append([InlineKeyboardButton("אחר / Other — הקלד שם אזור", callback_data="city:other")])
    return InlineKeyboardMarkup(buttons)


def area_keyboard(city_group: str) -> InlineKeyboardMarkup:
    """Second-level keyboard: choose a specific area within a city."""
    areas = CITY_AREAS[city_group]
    buttons = [
        [InlineKeyboardButton(area, callback_data=f"area:{area}")]
        for area in areas
    ]
    buttons.append([InlineKeyboardButton("אחר / Other — הקלד שם אזור", callback_data="city:other")])
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Bot command / callback handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "שלום! 👋\n\nבחר את העיר שלך כדי לקבל התרעות טילים:\n"
        "Hello! Choose your city to receive missile alerts:",
        reply_markup=city_keyboard(),
    )


async def cmd_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "בחר עיר חדשה / Choose a new city:",
        reply_markup=city_keyboard(),
    )


async def _register_area(query, chat_id: str, area: str) -> None:
    """Persist the area and confirm to the user."""
    users = load_users()
    users[chat_id] = area
    save_users(users)
    await query.edit_message_text(
        f"✅ נרשמת לקבלת התרעות עבור: {area}\n"
        f"You will receive alerts for: {area}\n\n"
        "כדי לשנות אזור, שלח /city"
    )
    logger.info("User %s registered for area: %s", chat_id, area)


async def handle_citygroup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """First-level selection: city group chosen."""
    query = update.callback_query
    await query.answer()

    city_group = query.data.removeprefix("citygroup:")
    areas = CITY_AREAS.get(city_group, [])

    if len(areas) == 1:
        # Only one area — register immediately without a second keyboard
        await _register_area(query, str(query.message.chat_id), areas[0])
    else:
        await query.edit_message_text(
            f"בחר אזור ב{city_group} / Choose an area in {city_group}:",
            reply_markup=area_keyboard(city_group),
        )


async def handle_area_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Second-level selection: specific area chosen."""
    query = update.callback_query
    await query.answer()
    area = query.data.removeprefix("area:")
    await _register_area(query, str(query.message.chat_id), area)


async def handle_other_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User chose 'Other' — prompt free-text entry."""
    query = update.callback_query
    await query.answer()
    WAITING_FOR_CITY.add(query.message.chat_id)
    await query.edit_message_text("הקלד את שם האזור בעברית:\nType the area name in Hebrew:")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in WAITING_FOR_CITY:
        return

    city = update.message.text.strip()
    WAITING_FOR_CITY.discard(chat_id)

    users = load_users()
    users[str(chat_id)] = city
    save_users(users)

    await update.message.reply_text(
        f"✅ נרשמת לקבלת התרעות עבור: {city}\n"
        f"You will receive alerts for: {city}\n\n"
        "כדי לשנות עיר, שלח /city"
    )
    logger.info("User %s registered for city (typed): %s", chat_id, city)


# ---------------------------------------------------------------------------
# oref.org.il polling
# ---------------------------------------------------------------------------


async def fetch_alerts(session: aiohttp.ClientSession) -> dict | None:
    try:
        async with session.get(OREF_API_URL, headers=OREF_HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning("oref API returned HTTP %s", resp.status)
                return None
            text = await resp.text(encoding="utf-8-sig")
            text = text.strip()
            if not text:
                return None
            return json.loads(text)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("Network error fetching alerts: %s", e)
        return None
    except json.JSONDecodeError as e:
        logger.warning("JSON decode error: %s", e)
        return None


def get_alerted_cities(alert_data: dict) -> set[str]:
    """Return the set of targeted cities/zones from active alerts.

    cat=1 is missile/rocket fire.  We forward *all* categories so that
    no real alert is silently dropped; non-cat-1 values are logged for
    investigation.
    """
    cat = str(alert_data.get("cat", ""))
    if cat != "1":
        logger.info("Alert with cat=%s (non-missile): %s", cat, alert_data.get("title", ""))
    cities = {normalize(city) for city in alert_data.get("data", []) if city.strip()}
    if cities:
        logger.info("Active alert cat=%s, cities=%s", cat, cities)
    return cities


def format_alert_message(location: str, alert_data: dict) -> str:
    title = alert_data.get("title", "ירי טילים ורקטות")
    desc = alert_data.get("desc", "היכנסו למרחב המוגן")
    cat = str(alert_data.get("cat", "1"))

    if cat == "14":
        emoji = "⚠️"
        en_title = "Early warning (pre-alert)"
    else:
        emoji = "🚨"
        en_title = title

    return (
        f"{emoji} התרעה: {title}\n"
        f"📍 אזור: {location}\n"
        f"⚠️ הנחיה: {desc}\n\n"
        f"{emoji} ALERT: {en_title}\n"
        f"📍 Area: {location}\n"
        f"⚠️ Action: {desc}"
    )


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

# {chat_id_str: last_alert_id_sent}
_sent_alerts: dict[str, str | None] = {}


async def poll_loop(application: Application) -> None:
    logger.info("Starting alert poll loop (interval=%ss)", POLL_INTERVAL)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                alert_data = await fetch_alerts(session)

                if alert_data is None:
                    # No active alert — reset dedup so next real alert fires
                    for key in list(_sent_alerts.keys()):
                        _sent_alerts[key] = None
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                alert_id = str(alert_data.get("id", "")) or str(alert_data.get("data", ""))
                alerted_cities = get_alerted_cities(alert_data)

                if not alerted_cities:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                users = load_users()
                if not users:
                    logger.warning("Alert fired but no users are registered.")
                for chat_id, location in users.items():
                    if not city_matches_alert(location, alerted_cities):
                        logger.debug(
                            "User %s location '%s' not in alerted cities %s",
                            chat_id, location, alerted_cities,
                        )
                        continue
                    if _sent_alerts.get(chat_id) == alert_id:
                        continue  # already sent this alert to this user

                    message = format_alert_message(location, alert_data)
                    try:
                        await application.bot.send_message(chat_id=int(chat_id), text=message)
                        _sent_alerts[chat_id] = alert_id
                        logger.info("Alert sent to user %s for location %s", chat_id, location)
                    except Exception as e:
                        logger.error("Failed to send alert to %s: %s", chat_id, e)

            except Exception as e:
                logger.error("Unexpected error in poll loop: %s", e)

            await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        sys.exit("ERROR: TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("city", cmd_city))
    app.add_handler(CallbackQueryHandler(handle_citygroup_callback, pattern=r"^citygroup:"))
    app.add_handler(CallbackQueryHandler(handle_area_callback, pattern=r"^area:"))
    app.add_handler(CallbackQueryHandler(handle_other_callback, pattern=r"^city:other$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
    await app.bot.set_my_commands([
        ("start", "Register / choose your alert area"),
        ("city", "Change your registered area"),
    ])
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    logger.info("Bot started. Polling for updates and alerts.")

    try:
        await poll_loop(app)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

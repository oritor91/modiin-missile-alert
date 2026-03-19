"""
Missile alert Telegram bot.

Polls the oref.org.il REST API for active rocket/missile alerts and notifies
registered users when their chosen city is targeted.

Users register via /start or /city and pick a city from an inline keyboard.
City selection is two-step: first pick a city, then pick a zone within it
(or choose "All zones").
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
OREF_CITIES_URL = "https://www.oref.org.il/Shared/Ajax/GetCitiesMix.aspx?lang=he"
OREF_HEADERS = {
    "Referer": "https://www.oref.org.il/",
    "X-Requested-With": "XMLHttpRequest",
}

# Preset cities shown on the inline keyboard
PRESET_CITIES = [
    "מודיעין-מכבים-רעות",
    "תל אביב - יפו",
    "ירושלים",
    "חיפה",
    "באר שבע",
    "ראשון לציון",
]

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
# User storage  {str(chat_id): city_or_zone_name}
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


def register_user(chat_id: str, location: str) -> None:
    users = load_users()
    users[chat_id] = location
    save_users(users)


# ---------------------------------------------------------------------------
# City / zone helpers
# ---------------------------------------------------------------------------

WAITING_FOR_CITY: set[int] = set()  # chat_ids that sent "Other"


def normalize(text: str) -> str:
    """NFC-normalize and strip so city comparisons are encoding-agnostic."""
    return unicodedata.normalize("NFC", text).strip()


def city_matches_alert(user_location: str, alerted_cities: set[str]) -> bool:
    """Return True if the user's registered location should receive this alert.

    Supports two registration types:
    - Whole city (e.g. "מודיעין-מכבים-רעות"): matches any zone of that city.
    - Specific zone (e.g. "מודיעין-מכבים-רעות - אזור א'"): exact match only.

    oref uses " - " as the separator between city and zone name.
    """
    norm = normalize(user_location)
    for alerted in alerted_cities:
        if alerted == norm:
            return True
        # Whole-city registration: match "CityName - <zone>"
        if alerted.startswith(norm + " - "):
            return True
    return False


def city_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(city, callback_data=f"city:{city}")]
        for city in PRESET_CITIES
    ]
    buttons.append([InlineKeyboardButton("אחר / Other — הקלד שם עיר", callback_data="city:other")])
    return InlineKeyboardMarkup(buttons)


def zone_keyboard(zones: list[str]) -> InlineKeyboardMarkup:
    """Build a keyboard with one button per zone.

    Callback data uses indices to stay well within Telegram's 64-byte limit.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(zone, callback_data=f"zone:{i}")]
        for i, zone in enumerate(zones)
    ])


async def fetch_city_zones(city: str) -> list[str]:
    """Return a sorted list of zones for *city* from the oref cities API.

    Returns an empty list on any error (caller falls back to whole-city registration).
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                OREF_CITIES_URL,
                headers=OREF_HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning("oref cities API returned HTTP %s", resp.status)
                    return []
                text = await resp.text(encoding="utf-8-sig")
                data = json.loads(text.strip())
                norm_city = normalize(city)
                zones = sorted({
                    normalize(item["label"])
                    for item in data
                    if isinstance(item.get("label"), str)
                    and normalize(item["label"]).startswith(norm_city + " - ")
                })
                return zones
    except Exception as e:
        logger.warning("Failed to fetch city zones for '%s': %s", city, e)
        return []


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


async def handle_city_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = str(query.message.chat_id)
    data = query.data  # "city:<name>" or "city:other"

    if data == "city:other":
        WAITING_FOR_CITY.add(query.message.chat_id)
        await query.edit_message_text("הקלד את שם העיר בעברית:\nType the city name in Hebrew:")
        return

    city = data.removeprefix("city:")

    # Try to fetch zones for this city so the user can pick one.
    zones = await fetch_city_zones(city)

    if zones:
        # Store zone list in chat_data so handle_zone_callback can look them up.
        context.chat_data["pending_zones"] = zones
        await query.edit_message_text(
            f"בחר אזור ב-{city}:\nChoose a zone in {city}:",
            reply_markup=zone_keyboard(zones),
        )
    else:
        # No sub-zones found — register for the whole city.
        register_user(chat_id, city)
        await query.edit_message_text(
            f"✅ נרשמת לקבלת התרעות עבור: {city}\n"
            f"You will receive alerts for: {city}\n\n"
            "כדי לשנות עיר, שלח /city"
        )
        logger.info("User %s registered for city: %s", chat_id, city)


async def handle_zone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = str(query.message.chat_id)
    zones: list[str] = context.chat_data.get("pending_zones", [])

    try:
        idx = int(query.data.removeprefix("zone:"))
        location = zones[idx]
    except (ValueError, IndexError):
        await query.edit_message_text("שגיאה — נסה שוב / Error — please try again.")
        return

    register_user(chat_id, location)
    context.chat_data.pop("pending_zones", None)

    await query.edit_message_text(
        f"✅ נרשמת לקבלת התרעות עבור: {location}\n"
        f"You will receive alerts for: {location}\n\n"
        "כדי לשנות עיר/אזור, שלח /city"
    )
    logger.info("User %s registered for location: %s", chat_id, location)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in WAITING_FOR_CITY:
        return

    city = update.message.text.strip()
    WAITING_FOR_CITY.discard(chat_id)

    register_user(str(chat_id), city)

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
    return (
        f"🚨 התרעה: {title}\n"
        f"📍 אזור: {location}\n"
        f"⚠️ הנחיה: {desc}\n\n"
        f"🚨 ALERT: Missile/Rocket fire\n"
        f"📍 Area: {location}\n"
        f"⚠️ Action: Enter the protected space immediately."
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
    app.add_handler(CallbackQueryHandler(handle_city_callback, pattern=r"^city:"))
    app.add_handler(CallbackQueryHandler(handle_zone_callback, pattern=r"^zone:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await app.initialize()
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

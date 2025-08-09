import os
import logging
import asyncio
import aiohttp
import discord
from discord.ext import tasks, commands
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta, timezone
import pytz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============== CONFIG ===============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_PRICE_CHANNEL_ID = int(os.getenv("BTC_PRICE_CHANNEL_ID"))
ETH_PRICE_CHANNEL_ID = int(os.getenv("ETH_PRICE_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
BTC_VOL_CHANNEL_ID = int(os.getenv("BTC_VOL_CHANNEL_ID"))
ETH_VOL_CHANNEL_ID = int(os.getenv("ETH_VOL_CHANNEL_ID"))
SESSIONS_CHANNEL_ID = int(os.getenv("SESSIONS_CHANNEL_ID"))
HEALTH_URL = os.getenv("HEALTH_URL")  # Для Koyeb Ping
# =====================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== Flask health server =====
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=8000)

Thread(target=run_flask, daemon=True).start()

# ===== Shared state =====
last_values = {
    "sessions_msg_id": None,
    "sessions_msg_content": None,
    "sessions_last_update": None,
}

# ===== Constants & Timezones =====
MIAMI_TZ = pytz.timezone("America/New_York")
LONDON_TZ = pytz.timezone("Europe/London")
TOKYO_TZ = pytz.timezone("Asia/Tokyo")

# ===== Market session schedule (время Майами) =====
# Все времена в local time Майами

# Время закрытия рынков перед выходными (Пятница 17:00)
MARKET_CLOSE_TIME = {"hour": 17, "minute": 0}

# Время открытия рынков после выходных (Воскресенье 17:00)
MARKET_OPEN_TIME = {"hour": 17, "minute": 0}

# Часы работы сессий (в часах) - ориентировочно
SESSIONS = {
    "Tokyo": {
        "open_hour": 19,   # 7 PM Майами время = 8 AM Tokyo следующего дня (примерно)
        "close_hour": 4,   # 4 AM Майами (следующий день)
        "timezone": TOKYO_TZ,
    },
    "London": {
        "open_hour": 3,    # 3 AM Майами (примерно 8 AM London)
        "close_hour": 12,  # 12 PM Майами (примерно 5 PM London)
        "timezone": LONDON_TZ,
    },
    "New York": {
        "open_hour": 8,    # 8 AM Майами
        "close_hour": 17,  # 5 PM Майами
        "timezone": MIAMI_TZ,
    },
}

def get_next_weekday(dt, weekday):
    """
    Возвращает datetime ближайшего будущего дня недели (0=Понедельник,...,6=Воскресенье)
    Если dt - нужный день, вернет dt.
    """
    days_ahead = (weekday - dt.weekday()) % 7
    return dt + timedelta(days=days_ahead)

def get_market_open_close_dt(market, now_miami):
    """
    Возвращает даты открытия и закрытия сессии в datetime (Майами), с учётом
    работы сессий внутри недели.
    """
    session = SESSIONS[market]

    # Если рынок сейчас в выходных (пятница 17:00 - воскресенье 17:00), возвращаем None для open/close

    friday_close = now_miami.replace(hour=MARKET_CLOSE_TIME["hour"], minute=MARKET_CLOSE_TIME["minute"], second=0, microsecond=0)
    friday_close = get_next_weekday(friday_close, 4)  # Ближайшая пятница

    sunday_open = now_miami.replace(hour=MARKET_OPEN_TIME["hour"], minute=MARKET_OPEN_TIME["minute"], second=0, microsecond=0)
    sunday_open = get_next_weekday(sunday_open, 6)  # Ближайшее воскресенье

    # Если сейчас после пятницы 17:00 и до воскресенья 17:00 — рынки закрыты
    if friday_close <= now_miami < sunday_open:
        return None, None

    # Определяем время открытия и закрытия для текущей недели

    # Даты открытия и закрытия для текущего дня
    open_dt = now_miami.replace(hour=session["open_hour"], minute=0, second=0, microsecond=0)
    close_dt = now_miami.replace(hour=session["close_hour"], minute=0, second=0, microsecond=0)

    # Для сессий с "закрытием" в следующем дне (Tokyo)
    if session["close_hour"] < session["open_hour"]:
        # close_dt переносим на следующий день
        close_dt += timedelta(days=1)

    # Если время открытия в прошлом, берём открытие следующего дня (рабочего)
    if open_dt <= now_miami:
        open_dt += timedelta(days=1)
        close_dt += timedelta(days=1)

    return open_dt, close_dt

def get_session_status(market, now_miami):
    """
    Возвращает статус (open/closed) и timedelta до открытия/закрытия
    """

    friday_close = now_miami.replace(hour=MARKET_CLOSE_TIME["hour"], minute=MARKET_CLOSE_TIME["minute"], second=0, microsecond=0)
    friday_close = get_next_weekday(friday_close, 4)  # ближайшая пятница 17:00

    sunday_open = now_miami.replace(hour=MARKET_OPEN_TIME["hour"], minute=MARKET_OPEN_TIME["minute"], second=0, microsecond=0)
    sunday_open = get_next_weekday(sunday_open, 6)  # ближайшее воскресенье 17:00

    # Если сейчас выходные - закрыты все рынки
    if friday_close <= now_miami < sunday_open:
        # Открытие для Токио только в воскресенье 17:00
        if market == "Tokyo":
            time_to_open = sunday_open - now_miami
            return "closed", time_to_open

        # London и New York — закрыты в выходные, откроются в воскресенье 17:00
        time_to_open = sunday_open - now_miami
        return "closed", time_to_open

    # Если время в будние, считаем локальный статус

    open_dt, close_dt = get_market_open_close_dt(market, now_miami)

    if open_dt is None or close_dt is None:
        # Если None, значит выходные
        return "closed", sunday_open - now_miami

    if open_dt <= now_miami < close_dt:
        # Рынок открыт
        time_to_close = close_dt - now_miami
        return "open", time_to_close
    else:
        # Рынок закрыт, ждем открытия
        time_to_open = open_dt - now_miami
        return "closed", time_to_open

def format_timedelta(delta):
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

def get_session_status_emoji(status, seconds_until):
    if status == "open":
        return "🟢"
    if status == "closed":
        if seconds_until <= 3600:
            return "🟡"
        return "🔴"
    return ""

def format_updated_since(last_update_dt, now_dt):
    if not last_update_dt:
        return "обновлено только что"
    diff = now_dt - last_update_dt
    seconds = diff.total_seconds()
    if seconds < 60:
        return "обновлено только что"
    elif seconds < 3600:
        mins = int(seconds // 60)
        return f"обновлено {mins} мин назад"
    else:
        hours = int(seconds // 3600)
        return f"обновлено {hours} ч назад"

async def update_sessions_message():
    now_utc = datetime.now(timezone.utc)
    now_miami = now_utc.astimezone(MIAMI_TZ).replace(second=0, microsecond=0)

    markets = ["Tokyo", "London", "New York"]
    sessions_info = {}

    for market in markets:
        status, delta = get_session_status(market, now_miami)
        sessions_info[market] = {
            "status": status,
            "relative": int(delta.total_seconds()),
            "formatted_delta": format_timedelta(delta),
        }

    updated_text = format_updated_since(last_values.get("sessions_last_update"), now_utc)
    header = f"🕒 Market sessions (relative times, America/New_York) — {updated_text}\n\n"

    lines = []
    for market in markets:
        info = sessions_info[market]
        emoji = get_session_status_emoji(info["status"], info["relative"])
        status_text = "open — closes in" if info["status"] == "open" else "closed — opens in"
        line = f"{emoji} {market}: {status_text} {info['formatted_delta']}"
        lines.append(line)

    footer = "\n\n⚠️ Countdown is relative (D days Hh Mm). Gap alerts posted for session opens."
    message = header + "\n".join(lines) + footer

    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not channel:
        logger.warning("Sessions channel not found")
        return

    try:
        msg_id = last_values.get("sessions_msg_id")
        msg = None
        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
            except discord.NotFound:
                msg = None
        if msg is None:
            msg = await channel.send(message)
            last_values["sessions_msg_id"] = msg.id
            last_values["sessions_msg_content"] = message
            last_values["sessions_last_update"] = now_utc
            logger.info("Sessions message sent (new)")
        else:
            if message != last_values.get("sessions_msg_content"):
                await msg.edit(content=message)
                last_values["sessions_msg_content"] = message
                last_values["sessions_last_update"] = now_utc
                logger.info("Sessions message updated")
    except Exception as e:
        logger.error(f"Failed to update sessions message: {e}")

# ===== Остальной твой код (fetchers, обновления каналов, таски, on_ready и т.п.) =====

# Например:
# async def update_prices(): ...
# async def update_fng(): ...
# async def health_ping(): ...
# и старт задач в on_ready

# ===== Запуск бота =====
@bot.event
async def on_ready():
    logger.info(f"✅ Bot started as {bot.user}")
    # Запуск твоих тасков обновления здесь
    # Например:
    # update_prices.start()
    # update_fng.start()
    # update_sessions.start()
    # health_ping.start()
    update_sessions.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set")
        exit(1)
    bot.run(DISCORD_TOKEN)

# ===== Таск update_sessions =====
from discord.ext import tasks

@tasks.loop(minutes=17)
async def update_sessions():
    try:
        await update_sessions_message()
    except Exception as e:
        logger.error(f"Error in update_sessions task: {e}")

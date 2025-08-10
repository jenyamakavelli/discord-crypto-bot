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

# ===== Helpers =====

def format_timedelta(delta):
    total_seconds = int(delta.total_seconds())
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

def format_updated_since(last_update_dt, now_dt):
    if last_update_dt is None:
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

def get_session_status_emoji(status, relative_seconds):
    if status == "open":
        return "🟢"
    elif status == "closed":
        if relative_seconds <= 3600:
            return "🟡"
        return "🔴"
    return ""

# ===== Timezones =====
MIAMI_TZ = pytz.timezone("America/New_York")
EUROPE_TZ = pytz.timezone("Europe/Berlin")  # CET/CEST с автоматическим переходом

# ===== Forex sessions definitions =====
# Время открытия в ET (America/New_York), время закрытия считается + продолжительность сессии
# Длительность сессий (часы)
SESSION_DEFS = {
    "Pacific": {
        "open_hour": 17,
        "open_minute": 0,
        "duration_hours": 9,  # 17:00–02:00 ET
        "symbols": ["AUD", "NZD"]
    },
    "Tokyo": {
        "open_hour": 19,
        "open_minute": 0,
        "duration_hours": 9,  # 19:00–04:00 ET
        "symbols": ["JPY", "CNY", "SGD", "HKD"]
    },
    "European": {
        "open_hour": 3,
        "open_minute": 0,
        "duration_hours": 9,  # 03:00–12:00 ET
        "symbols": ["EUR", "GBP", "CHF"]
    },
    "American": {
        "open_hour": 8,
        "open_minute": 30,
        "duration_hours": 8.5,  # 08:30–17:00 ET
        "symbols": ["USD"]
    },
}

def get_last_open_close(now, open_hour, open_minute, duration_hours):
    """Определяет последнее открытие и закрытие сессии относительно now в MIAMI_TZ"""
    # Сдвиг на сегодня с нужным временем
    today_open = now.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)

    if now < today_open:
        # Если сейчас раньше открытия, берем открытие вчера
        last_open = today_open - timedelta(days=1)
    else:
        last_open = today_open

    last_close = last_open + timedelta(hours=duration_hours)
    return last_open, last_close

def get_sessions_status(now_utc):
    now_miami = now_utc.astimezone(MIAMI_TZ).replace(second=0, microsecond=0)
    now_europe = now_utc.astimezone(EUROPE_TZ)

    result = {}

    for session_name, params in SESSION_DEFS.items():
        last_open, last_close = get_last_open_close(
            now_miami,
            params["open_hour"],
            params["open_minute"],
            params["duration_hours"]
        )

        if last_open <= now_miami < last_close:
            status = "open"
            delta = last_close - now_miami
        else:
            status = "closed"
            # Следующее открытие через 1 день (24 часа)
            next_open = last_open + timedelta(days=1)
            if now_miami >= last_close:
                # Если после закрытия, следующая сессия завтра
                delta = next_open - now_miami
            else:
                # Если до открытия сегодня (до last_open), значит текущее время до открытия
                delta = last_open - now_miami

        # Форматируем время открытия и закрытия по ET и CET/CEST
        open_dt_miami = last_open
        close_dt_miami = last_close

        open_dt_europe = open_dt_miami.astimezone(EUROPE_TZ)
        close_dt_europe = close_dt_miami.astimezone(EUROPE_TZ)

        result[session_name] = {
            "status": status,
            "relative_seconds": int(delta.total_seconds()),
            "formatted_delta": format_timedelta(delta),
            "symbols": params["symbols"],
            "open_time_miami": open_dt_miami.strftime("%H:%M"),
            "close_time_miami": close_dt_miami.strftime("%H:%M"),
            "open_time_europe": open_dt_europe.strftime("%H:%M"),
            "close_time_europe": close_dt_europe.strftime("%H:%M"),
        }

    return result

async def update_sessions_message():
    now_utc = datetime.now(timezone.utc)
    sessions_info = get_sessions_status(now_utc)

    updated_text = format_updated_since(last_values.get("sessions_last_update"), now_utc)
    header = f"🕒 Форекс сессии (обновлено {updated_text})\n\n"

    lines = []
    # Для сортировки в нужном порядке
    order = ["Pacific", "Tokyo", "European", "American"]
    for session_name in order:
        info = sessions_info[session_name]
        emoji = get_session_status_emoji(info["status"], info["relative_seconds"])
        status_short = "откр." if info["status"] == "open" else "закр."
        line = f"{emoji}  {session_name}:  {status_short} — "
        line += f"{'закроется' if info['status'] == 'open' else 'откроется'} через  {info['formatted_delta']}  "
        line += f"[{', '.join(info['symbols'])}]"
        lines.append(line)

    footer = "\n───────────────\n\n"
    footer += "📊 Время сессий (открытие — закрытие):\n\n"
    # Вывод времени по Майами и Европе в компактном виде
    for session_name in order:
        info = sessions_info[session_name]
        # Выравнивание по длине самого длинного названия (European)
        footer += f"{session_name:<9}: 🇺🇸 {info['open_time_miami']}–{info['close_time_miami']}  |  🇪🇺 {info['open_time_europe']}–{info['close_time_europe']}\n"

    content = header + "\n".join(lines) + footer

    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not channel:
        logger.error("Sessions channel not found")
        return

    if last_values.get("sessions_msg_id"):
        try:
            msg = await channel.fetch_message(last_values["sessions_msg_id"])
            if msg.content != content:
                await msg.edit(content=content)
                last_values["sessions_msg_content"] = content
                last_values["sessions_last_update"] = now_utc
                logger.info("Обновлено сообщение сессий")
        except (discord.NotFound, discord.Forbidden):
            msg = await channel.send(content)
            last_values["sessions_msg_id"] = msg.id
            last_values["sessions_msg_content"] = content
            last_values["sessions_last_update"] = now_utc
            logger.info("Создано новое сообщение сессий")
    else:
        msg = await channel.send(content)
        last_values["sessions_msg_id"] = msg.id
        last_values["sessions_msg_content"] = content
        last_values["sessions_last_update"] = now_utc
        logger.info("Создано новое сообщение сессий")

# ===== Background tasks =====
@tasks.loop(seconds=60)
async def update_sessions():
    try:
        await update_sessions_message()
    except Exception as e:
        logger.error(f"Error in update_sessions task: {e}")

# ===== Startup =====
@bot.event
async def on_ready():
    logger.info(f"✅ Bot started as {bot.user}")
    update_sessions.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set")
        exit(1)
    bot.run(DISCORD_TOKEN)

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

# ---------- logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("forex-sessions")

# =============== CONFIG ===============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_PRICE_CHANNEL_ID = int(os.getenv("BTC_PRICE_CHANNEL_ID", "0"))
ETH_PRICE_CHANNEL_ID = int(os.getenv("ETH_PRICE_CHANNEL_ID", "0"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID", "0"))
BTC_VOL_CHANNEL_ID = int(os.getenv("BTC_VOL_CHANNEL_ID", "0"))
ETH_VOL_CHANNEL_ID = int(os.getenv("ETH_VOL_CHANNEL_ID", "0"))
SESSIONS_CHANNEL_ID = int(os.getenv("SESSIONS_CHANNEL_ID", "0"))
HEALTH_URL = os.getenv("HEALTH_URL")  # Для автопинга
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
def format_timedelta(delta: timedelta) -> str:
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

def format_updated_since(last_update_dt, now_dt):
    if last_update_dt is None:
        return "только что"
    diff = now_dt - last_update_dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "только что"
    elif seconds < 3600:
        mins = int(seconds // 60)
        return f"{mins} мин назад"
    else:
        hours = int(seconds // 3600)
        return f"{hours} ч назад"

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
EUROPE_TZ = pytz.timezone("Europe/Berlin")

# ===== Forex sessions definitions =====
SESSION_DEFS = {
    "Pacific": {"open_hour": 17, "open_minute": 0, "duration_hours": 9.0, "symbols": ["AUD", "NZD"]},
    "Tokyo": {"open_hour": 19, "open_minute": 0, "duration_hours": 9.0, "symbols": ["JPY", "CNY", "SGD", "HKD"]},
    "European": {"open_hour": 3, "open_minute": 0, "duration_hours": 9.0, "symbols": ["EUR", "GBP", "CHF"]},
    "American": {"open_hour": 8, "open_minute": 30, "duration_hours": 8.5, "symbols": ["USD"]},
}

def get_last_open_close(now_miami, open_hour, open_minute, duration_hours):
    weekday = now_miami.weekday()  # 0=Mon ... 6=Sun

    # ===== Выходные =====
    if weekday >= 5:  # Saturday/Sunday
        # Следующее открытие в понедельник
        days_until_monday = 7 - weekday
        next_open = now_miami.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0) + timedelta(days=days_until_monday)
        last_open = now_miami - timedelta(hours=duration_hours)  # прошлое закрытие
        last_close = last_open + timedelta(hours=duration_hours)
        return last_open, next_open

    # ===== Рабочий день =====
    open_today = now_miami.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
    if now_miami >= open_today:
        last_open = open_today
    else:
        last_open = open_today - timedelta(days=1)
    last_close = last_open + timedelta(hours=duration_hours)
    return last_open, last_close

def get_sessions_status(now_utc):
    now_miami = now_utc.astimezone(MIAMI_TZ).replace(second=0, microsecond=0)
    result = {}
    for name, params in SESSION_DEFS.items():
        last_open, last_close = get_last_open_close(now_miami, params["open_hour"], params["open_minute"], params["duration_hours"])
        if last_open <= now_miami < last_close:
            status = "open"
            delta = last_close - now_miami
        else:
            status = "closed"
            if now_miami < last_open:
                delta = last_open - now_miami
            else:
                delta = timedelta(seconds=0)
        open_time_miami = last_open.strftime("%H:%M")
        close_time_miami = last_close.strftime("%H:%M")
        open_time_eu = last_open.astimezone(EUROPE_TZ).strftime("%H:%M")
        close_time_eu = last_close.astimezone(EUROPE_TZ).strftime("%H:%M")
        result[name] = {
            "status": status,
            "relative_seconds": int(delta.total_seconds()),
            "formatted_delta": format_timedelta(delta),
            "symbols": params["symbols"],
            "open_time_miami": open_time_miami,
            "close_time_miami": close_time_miami,
            "open_time_europe": open_time_eu,
            "close_time_europe": close_time_eu,
        }
    return result

async def update_sessions_message():
    now_utc = datetime.now(timezone.utc)
    sessions_info = get_sessions_status(now_utc)
    updated_text = format_updated_since(last_values.get("sessions_last_update"), now_utc)
    header = f"🕒 Форекс сессии (обновлено {updated_text})\n\n"
    order = ["Pacific", "Tokyo", "European", "American"]
    lines = []
    for name in order:
        info = sessions_info[name]
        emoji = get_session_status_emoji(info["status"], info["relative_seconds"])
        status_short = "откр." if info["status"] == "open" else "закр."
        action = "закроется" if info["status"] == "open" else "откроется"
        line = f"{emoji}  {name}:  {status_short} — {action} через  {info['formatted_delta']}  [{', '.join(info['symbols'])}]"
        lines.append(line)
    footer_lines = []
    footer_lines.append("\n───────────────\n")
    footer_lines.append("📊 Время сессий (открытие — закрытие):\n")
    for name in order:
        info = sessions_info[name]
        footer_lines.append(f"{name:<9}: 🇺🇸 {info['open_time_miami']}–{info['close_time_miami']}  |  🇪🇺 {info['open_time_europe']}–{info['close_time_europe']}")
    content = header + "\n".join(lines) + "\n\n" + "\n".join(footer_lines)
    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not channel:
        logger.error("Sessions channel not found (SESSIONS_CHANNEL_ID=%s)", SESSIONS_CHANNEL_ID)
        return
    now_for_store = now_utc
    if last_values.get("sessions_msg_id"):
        try:
            msg = await channel.fetch_message(last_values["sessions_msg_id"])
            if msg.content != content:
                await msg.edit(content=content)
                last_values["sessions_msg_content"] = content
                last_values["sessions_last_update"] = now_for_store
                logger.info("Updated sessions message")
        except (discord.NotFound, discord.Forbidden) as e:
            logger.warning("Previous sessions message not found or forbidden, creating new one: %s", e)
            msg = await channel.send(content)
            last_values["sessions_msg_id"] = msg.id
            last_values["sessions_msg_content"] = content
            last_values["sessions_last_update"] = now_for_store
            logger.info("Created new sessions message")
    else:
        msg = await channel.send(content)
        last_values["sessions_msg_id"] = msg.id
        last_values["sessions_msg_content"] = content
        last_values["sessions_last_update"] = now_for_store
        logger.info("Created sessions message")

# ===== Здесь остальной код (BTC/ETH цены, FNG и т.д.) без изменений =====

# ===== Background tasks =====
@tasks.loop(seconds=60)
async def update_sessions():
    try:
        await update_sessions_message()
    except Exception as e:
        logger.exception(f"Error in update_sessions task: {e}")

# ===== Startup =====
@bot.event
async def on_ready():
    logger.info(f"Bot started as {bot.user}")
    if not update_sessions.is_running():
        update_sessions.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN)

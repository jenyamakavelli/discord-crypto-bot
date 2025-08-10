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
    "btc_price": None,
    "eth_price": None,
    "btc_vol": None,
    "eth_vol": None,
    "fng": None,
    "sessions_msg_id": None,
    "sessions_msg_content": None,
    "sessions_last_update": None,
}

# ===== HTTP fetch with retry =====
async def fetch_json(session, url, max_retries=5):
    backoff = 1
    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", backoff))
                    logger.warning(f"429 rate limited by API, sleeping {retry_after} sec")
                    await asyncio.sleep(retry_after)
                    backoff = min(backoff * 2, 60)
                    continue
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"HTTP error {e} on attempt {attempt+1} for URL {url}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    logger.error(f"Failed to fetch {url} after {max_retries} attempts")
    return None

# ===== Data fetchers =====
async def get_price_and_volume(session, coin_id):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
    data = await fetch_json(session, url)
    if not data:
        return None, None
    try:
        price = data["market_data"]["current_price"]["usd"]
        volume = data["market_data"]["total_volume"]["usd"]
        return price, volume
    except KeyError:
        logger.warning(f"Malformed data from CoinGecko for {coin_id}")
        return None, None

async def get_fear_and_greed(session):
    url = "https://api.alternative.me/fng/"
    data = await fetch_json(session, url)
    if not data:
        return None
    try:
        return int(data["data"][0]["value"])
    except (KeyError, IndexError, ValueError):
        logger.warning("Malformed Fear & Greed Index data")
        return None

# ===== Helpers =====
def format_volume(vol):
    if vol >= 1_000_000_000:
        return f"${vol/1_000_000_000:.1f}B"
    elif vol >= 1_000_000:
        return f"${vol/1_000_000:.1f}M"
    else:
        return f"${vol:,.0f}"

async def update_channel_if_changed(channel_id, new_name, key):
    if last_values.get(key) != new_name:
        channel = bot.get_channel(channel_id)
        if channel:
            try:
                await channel.edit(name=new_name)
                last_values[key] = new_name
                logger.info(f"Обновлен канал {channel_id}: {new_name}")
            except discord.HTTPException as e:
                logger.error(f"Ошибка обновления канала {channel_id}: {e}")

# ===== Time zone and sessions =====
MIAMI_TZ = pytz.timezone("America/New_York")
EUROPE_TZ = pytz.timezone("Europe/London")

SESSIONS = {
    "Pacific": {"open_hour": 17, "close_hour": 2, "symbols": ["AUD", "NZD"]},     # 17:00–02:00 ET
    "Tokyo":   {"open_hour": 19, "close_hour": 4, "symbols": ["JPY", "CNY", "SGD", "HKD"]},  # 19:00–04:00 ET
    "European":{"open_hour": 3,  "close_hour": 12, "symbols": ["EUR", "GBP", "CHF"]},       # 03:00–12:00 ET
    "American":{"open_hour": 8,  "close_hour": 17, "symbols": ["USD"]},                    # 08:00–17:00 ET
}

def get_session_times(now, market):
    open_hour = SESSIONS[market]["open_hour"]
    close_hour = SESSIONS[market]["close_hour"]

    # Определяем дату открытия сессии сегодня или ранее, для now в часовом поясе Майами
    open_time = now.replace(hour=open_hour, minute=0, second=0, microsecond=0)

    # Если сейчас раньше открытия сессии — переходим к предыдущему дню открытия
    if now < open_time:
        open_time -= timedelta(days=1)

    # Закрытие сессии с учётом перехода через полночь
    if close_hour <= open_hour:
        close_time = open_time + timedelta(days=1, hours=close_hour - open_hour)
    else:
        close_time = open_time.replace(hour=close_hour)

    return open_time, close_time

def get_sessions_status(now_utc):
    now_miami = now_utc.astimezone(MIAMI_TZ).replace(second=0, microsecond=0)

    result = {}
    for market in SESSIONS:
        open_time, close_time = get_session_times(now_miami, market)

        if open_time <= now_miami < close_time:
            status = "open"
            delta = close_time - now_miami
        else:
            status = "closed"
            # Следующее открытие сессии
            next_open = open_time + timedelta(days=1)
            delta = next_open - now_miami

        result[market] = {
            "status": status,
            "relative_seconds": int(delta.total_seconds()),
            "formatted_delta": format_timedelta(delta),
        }
    return result

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

def get_session_status_emoji(status, relative_seconds):
    if status == "open":
        return "🟢"
    elif status == "closed":
        if relative_seconds <= 3600:
            return "🟡"
        return "🔴"
    return ""

def format_updated_since(last_update_dt, now_dt):
    if last_update_dt is None:
        return "обновлено только что"
    diff = now_dt - last_update_dt
    seconds = diff.total_seconds()
    if seconds < 60:
        return "обновлено только что"
    elif seconds < 3600:
        mins = int(seconds // 60)
        if mins == 1:
            return "1 мин назад"
        return f"{mins} мин назад"
    else:
        hours = int(seconds // 3600)
        if hours == 1:
            return "1 ч назад"
        return f"{hours} ч назад"

def build_sessions_time_summary():
    lines = []
    now_utc = datetime.now(timezone.utc)
    now_miami = now_utc.astimezone(MIAMI_TZ)
    for market in ["Pacific", "Tokyo", "European", "American"]:
        open_hour = SESSIONS[market]["open_hour"]
        close_hour = SESSIONS[market]["close_hour"]

        open_dt_et = now_miami.replace(hour=open_hour, minute=0, second=0, microsecond=0)
        if now_miami < open_dt_et:
            open_dt_et -= timedelta(days=1)

        if close_hour <= open_hour:
            close_dt_et = open_dt_et + timedelta(days=1, hours=close_hour - open_hour)
        else:
            close_dt_et = open_dt_et.replace(hour=close_hour)

        open_dt_eu = open_dt_et.astimezone(EUROPE_TZ)
        close_dt_eu = close_dt_et.astimezone(EUROPE_TZ)

        et_open_str = open_dt_et.strftime("%H:%M")
        et_close_str = close_dt_et.strftime("%H:%M")
        eu_open_str = open_dt_eu.strftime("%H:%M")
        eu_close_str = close_dt_eu.strftime("%H:%M")

        lines.append(f"{market:<9}: 🇺🇸 {et_open_str}–{et_close_str}  |  🇪🇺 {eu_open_str}–{eu_close_str}")
    return "\n".join(lines)

async def update_sessions_message():
    now_utc = datetime.now(timezone.utc)
    sessions_info = get_sessions_status(now_utc)

    updated_text = format_updated_since(last_values.get("sessions_last_update"), now_utc)
    if updated_text.startswith("обновлено"):
        header = f"🕒 Форекс сессии ({updated_text})\n\n"
    else:
        header = f"🕒 Форекс сессии (обновлено {updated_text})\n\n"

    lines = []
    for market, info in sessions_info.items():
        emoji = get_session_status_emoji(info["status"], info["relative_seconds"])
        symbols = ", ".join(SESSIONS[market]["symbols"])
        status_short = "откр." if info["status"] == "open" else "закр."
        action = "закроется" if info["status"] == "open" else "откроется"
        line = f"{emoji}  {market}:  {status_short} — {action} через  {info['formatted_delta']}  [{symbols}]"
        lines.append(line)

    footer = "\n───────────────\n\n" + "📊 Время сессий (открытие — закрытие):\n\n" + build_sessions_time_summary()

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
@tasks.loop(minutes=6)
async def update_prices():
    async with aiohttp.ClientSession() as session:
        btc_price, btc_vol = await get_price_and_volume(session, "bitcoin")
        eth_price, eth_vol = await get_price_and_volume(session, "ethereum")

        if btc_price is not None:
            await update_channel_if_changed(BTC_PRICE_CHANNEL_ID, f"BTC: ${btc_price:,.2f}", "btc_price")
        if eth_price is not None:
            await update_channel_if_changed(ETH_PRICE_CHANNEL_ID, f"ETH: ${eth_price:,.2f}", "eth_price")

        if btc_vol is not None:
            await update_channel_if_changed(BTC_VOL_CHANNEL_ID, f"BTC Vol: {format_volume(btc_vol)}", "btc_vol")
        if eth_vol is not None:
            await update_channel_if_changed(ETH_VOL_CHANNEL_ID, f"ETH Vol: {format_volume(eth_vol)}", "eth_vol")

@tasks.loop(minutes=43)
async def update_fng():
    async with aiohttp.ClientSession() as session:
        fng = await get_fear_and_greed(session)
        if fng is not None:
            await update_channel_if_changed(FNG_CHANNEL_ID, f"Fear & Greed: {fng}", "fng")

@tasks.loop(minutes=5)
async def health_ping():
    if not HEALTH_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(HEALTH_URL) as resp:
                if resp.status == 200:
                    logger.info("✅ HEALTH URL pinged")
                else:
                    logger.warning(f"Health ping returned status {resp.status}")
    except Exception as e:
        logger.error(f"Health ping failed: {e}")

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
    update_prices.start()
    update_fng.start()
    update_sessions.start()
    health_ping.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set")
        exit(1)
    bot.run(DISCORD_TOKEN)

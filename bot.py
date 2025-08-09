import os
import logging
import asyncio
import aiohttp
import discord
from discord.ext import tasks, commands
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta, timezone

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

# ===== Shared state for last values to avoid redundant updates =====
last_values = {
    "btc_price": None,
    "eth_price": None,
    "btc_vol": None,
    "eth_vol": None,
    "fng": None,
    "sessions_message_id": None,
    "sessions_last_update": None,
}

# ===== Async HTTP fetch with retry and backoff for rate limits =====
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

# ===== Helper to format volume =====
def format_volume(vol):
    if vol >= 1_000_000_000:
        return f"${vol/1_000_000_000:.1f}B"
    elif vol >= 1_000_000:
        return f"${vol/1_000_000:.1f}M"
    else:
        return f"${vol:,.0f}"

# ===== Update channel only if value changed =====
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

# ===== Forex sessions config (UTC times) =====
SESSIONS = {
    "Tokyo": {"open": 0, "close": 9},     # 00:00 - 09:00 UTC
    "London": {"open": 8, "close": 17},   # 08:00 - 17:00 UTC
    "New York": {"open": 13, "close": 22} # 13:00 - 22:00 UTC
}

def format_timedelta(td):
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes = remainder // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

def next_weekday(d, weekday):
    days_ahead = weekday - d.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)

def get_session_status(now_utc, open_hour, close_hour):
    open_time = now_utc.replace(hour=open_hour, minute=0, second=0, microsecond=0)
    close_time = now_utc.replace(hour=close_hour, minute=0, second=0, microsecond=0)

    if now_utc >= close_time:
        open_time += timedelta(days=1)
        close_time += timedelta(days=1)

    # Перенос открытий, если попадают на выходные
    while open_time.weekday() in (5, 6):  # Sat=5, Sun=6
        open_time = next_weekday(open_time, 0).replace(hour=open_hour, minute=0)
        close_time = open_time + timedelta(hours=close_hour - open_hour)

    if open_time <= now_utc < close_time:
        status = "open"
        time_to_close = close_time - now_utc
        relative = format_timedelta(time_to_close)
        return status, f"closes in {relative}"
    else:
        status = "closed"
        if now_utc < open_time:
            time_to_open = open_time - now_utc
            relative = format_timedelta(time_to_open)
            return status, f"opens in {relative}"
        else:
            time_to_open = open_time - now_utc
            relative = format_timedelta(time_to_open)
            return status, f"opens in {relative}"

def get_session_status_emoji(status, relative):
    if status == "open":
        return "🟢"
    elif status == "closed" and relative.startswith("opens in 0h"):
        return "🟡"
    else:
        return "🔴"

def format_updated_since(last_update_dt, now):
    diff = now - last_update_dt
    minutes = int(diff.total_seconds() // 60)
    if minutes == 0:
        return "обновлено менее минуты назад"
    elif minutes == 1:
        return "обновлено 1 мин назад"
    else:
        return f"обновлено {minutes} мин назад"

# ===== Update pinned sessions message =====
async def update_sessions_message():
    now = datetime.utcnow().replace(second=0, microsecond=0)
    last_update = last_values.get("sessions_last_update", None)
    if not last_update:
        last_update = now
    else:
        # last_update хранится как datetime
        pass

    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not channel:
        logger.error("Sessions channel not found")
        return

    # Получаем pinned сообщения, ищем наше по ID
    pinned = await channel.pins()
    message = None
    if last_values.get("sessions_message_id"):
        for m in pinned:
            if m.id == last_values["sessions_message_id"]:
                message = m
                break

    if not message and pinned:
        # Если ID нет, берем первое pinned сообщение
        message = pinned[0]
        last_values["sessions_message_id"] = message.id

    lines = []
    for session_name, hours in SESSIONS.items():
        status, relative = get_session_status(now, hours["open"], hours["close"])
        emoji = get_session_status_emoji(status, relative)
        lines.append(f"{emoji} {session_name}: {status} — {relative}")

    updated_line = f"🕒 Market sessions (relative times, UTC) — {format_updated_since(last_update, now)}"
    footer = "\n\n⚠️ Countdown is relative (D days Hh Mm). Gap alerts posted for session opens."

    message_text = updated_line + "\n\n" + "\n".join(lines) + footer

    try:
        if message:
            await message.edit(content=message_text)
        else:
            msg = await channel.send(message_text)
            last_values["sessions_message_id"] = msg.id
        last_values["sessions_last_update"] = now
        logger.info("Sessions message updated")
    except Exception as e:
        logger.error(f"Failed to update sessions message: {e}")

# ===== Tasks =====
@tasks.loop(minutes=6)
async def update_prices():
    async with aiohttp.ClientSession() as session:
        btc_price, _ = await get_price_and_volume(session, "bitcoin")
        eth_price, _ = await get_price_and_volume(session, "ethereum")
        if btc_price is not None:
            await update_channel_if_changed(BTC_PRICE_CHANNEL_ID, f"BTC: ${btc_price:,.2f}", "btc_price")
        if eth_price is not None:
            await update_channel_if_changed(ETH_PRICE_CHANNEL_ID, f"ETH: ${eth_price:,.2f}", "eth_price")

@tasks.loop(minutes=17)
async def update_volumes():
    async with aiohttp.ClientSession() as session:
        _, btc_vol = await get_price_and_volume(session, "bitcoin")
        _, eth_vol = await get_price_and_volume(session, "ethereum")
        if btc_vol is not None:
            await update_channel_if_changed(BTC_VOL_CHANNEL_ID, f"BTC Vol: {format_volume(btc_vol)}", "btc_vol")
        if eth_vol is not None:
            await update_channel_if_changed(ETH_VOL_CHANNEL_ID, f"ETH Vol: {format_volume(eth_vol)}", "eth_vol")

@tasks.loop(minutes=43)
async def update_fng():
    async with aiohttp.ClientSession() as session:
        fng_value = await get_fear_and_greed(session)
        if fng_value is not None:
            await update_channel_if_changed(FNG_CHANNEL_ID, f"Fear & Greed: {fng_value}", "fng")

@tasks.loop(minutes=1)
async def update_sessions():
    await update_sessions_message()

@tasks.loop(minutes=5)
async def ping_health_url():
    if HEALTH_URL:
        async with aiohttp.ClientSession() as session:
            try:
                await session.get(HEALTH_URL, timeout=5)
                logger.info("Health URL pinged")
            except Exception as e:
                logger.warning(f"Failed to ping health URL: {e}")

@bot.event
async def on_ready():
    logger.info(f"✅ Bot started as {bot.user}")
    update_prices.start()
    update_volumes.start()
    update_fng.start()
    update_sessions.start()
    ping_health_url.start()

bot.run(DISCORD_TOKEN)

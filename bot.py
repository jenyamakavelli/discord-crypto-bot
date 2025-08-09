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
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID"))
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
    "sessions_msg_id": None,
    "news_msg_id": None,
    # Add more if needed
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

# ===== Market Sessions Logic =====

MIAMI_TZ = pytz.timezone("America/New_York")

def get_miami_now():
    return datetime.now(MIAMI_TZ).replace(second=0, microsecond=0)

def get_next_weekday(dt, weekday):
    days_ahead = weekday - dt.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return dt + timedelta(days=days_ahead)

def get_market_status(market_name, now_miami):
    # Все рынки закрыты с пятницы 17:00 по Майами до воскресенья 17:00 по Майами
    # Открыты в остальное время
    weekday = now_miami.weekday()
    hour = now_miami.hour
    minute = now_miami.minute

    # Пятница 17:00 Майами
    friday_17 = now_miami.replace(hour=17, minute=0, second=0, microsecond=0)
    friday_close = get_next_weekday(friday_17, 4)  # пятница

    # Воскресенье 17:00 Майами
    sunday_17 = now_miami.replace(hour=17, minute=0, second=0, microsecond=0)
    sunday_open = get_next_weekday(sunday_17, 6)  # воскресенье

    # Если сейчас в период закрытия выходных (пт 17:00 - вс 17:00)
    if friday_close <= now_miami < sunday_open:
        delta = sunday_open - now_miami
        return "closed", delta

    # Если воскресенье до 17:00
    if weekday == 6 and now_miami < sunday_open:
        delta = sunday_open - now_miami
        return "closed", delta

    # В другое время открыт
    # Следующее закрытие пятница 17:00
    next_friday = get_next_weekday(now_miami, 4).replace(hour=17, minute=0, second=0, microsecond=0)
    delta = next_friday - now_miami
    return "open", delta

def format_timedelta(delta: timedelta):
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes = remainder // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

def get_session_status_emoji(status: str, delta: timedelta):
    # 🟢 open, 🔴 closed, 🟡 скоро открытие (менее 1 часа)
    if status == "open":
        return "🟢"
    elif status == "closed":
        if delta.total_seconds() <= 3600:
            return "🟡"
        else:
            return "🔴"
    else:
        return ""

def format_updated_since(last_update_dt: datetime, now_dt: datetime):
    delta = now_dt - last_update_dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "обновлено только что"
    elif seconds < 3600:
        mins = seconds // 60
        return f"обновлено {mins} мин назад"
    elif seconds < 86400:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        if mins == 0:
            return f"обновлено {hours} ч назад"
        else:
            return f"обновлено {hours} ч {mins} мин назад"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"обновлено {days} д {hours} ч назад"

async def update_sessions_message():
    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if channel is None:
        logger.error("SESSIONS_CHANNEL_ID неверен или бот не имеет доступа")
        return

    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc, second=0, microsecond=0)
    now_miami = get_miami_now()

    markets = ["Tokyo", "London", "New York"]
    sessions_lines = []

    for market in markets:
        status, delta = get_market_status(market, now_miami)
        emoji = get_session_status_emoji(status, delta)
        time_str = format_timedelta(delta)
        if status == "open":
            sessions_lines.append(f"{emoji} {market}: open — closes in {time_str}")
        else:
            sessions_lines.append(f"{emoji} {market}: closed — opens in {time_str}")

    # Для строки обновления показываем "обновлено N мин назад"
    # Используем last update time, храним в last_values
    last_update_dt = last_values.get("sessions_last_update")
    if last_update_dt is None:
        last_update_dt = now_utc
    updated_text = format_updated_since(last_update_dt, now_utc)

    message_content = (
        f"🕒 Market sessions (relative times, UTC) — {updated_text}\n\n"
        + "\n".join(sessions_lines)
        + "\n\n⚠️ Countdown is relative (D days Hh Mm). Gap alerts posted for session opens."
    )

    # Отправляем или редактируем сообщение сессий
    if last_values.get("sessions_msg_id"):
        try:
            msg = await channel.fetch_message(last_values["sessions_msg_id"])
            await msg.edit(content=message_content)
        except discord.NotFound:
            msg = await channel.send(message_content)
            last_values["sessions_msg_id"] = msg.id
    else:
        msg = await channel.send(message_content)
        last_values["sessions_msg_id"] = msg.id

    last_values["sessions_last_update"] = now_utc
    logger.info("Sessions message updated")

# ===== Background tasks =====

@tasks.loop(minutes=6)
async def update_prices():
    async with aiohttp.ClientSession() as session:
        btc_price, _ = await get_price_and_volume(session, "bitcoin")
        eth_price, _ = await get_price_and_volume(session, "ethereum")

        if btc_price is not None:
            name = f"BTC: ${btc_price:,.2f}"
            await update_channel_if_changed(BTC_PRICE_CHANNEL_ID, name, "btc_price")
        if eth_price is not None:
            name = f"ETH: ${eth_price:,.2f}"
            await update_channel_if_changed(ETH_PRICE_CHANNEL_ID, name, "eth_price")

@tasks.loop(minutes=17)
async def update_volumes():
    async with aiohttp.ClientSession() as session:
        _, btc_vol = await get_price_and_volume(session, "bitcoin")
        _, eth_vol = await get_price_and_volume(session, "ethereum")

        if btc_vol is not None:
            name = f"BTC Vol: {format_volume(btc_vol)}"
            await update_channel_if_changed(BTC_VOL_CHANNEL_ID, name, "btc_vol")
        if eth_vol is not None:
            name = f"ETH Vol: {format_volume(eth_vol)}"
            await update_channel_if_changed(ETH_VOL_CHANNEL_ID, name, "eth_vol")

@tasks.loop(minutes=43)
async def update_fng():
    async with aiohttp.ClientSession() as session:
        fng_index = await get_fear_and_greed(session)
        if fng_index is not None:
            name = f"Fear & Greed: {fng_index}"
            await update_channel_if_changed(FNG_CHANNEL_ID, name, "fng")

@tasks.loop(minutes=1)
async def update_sessions():
    try:
        await update_sessions_message()
    except Exception as e:
        logger.error(f"Ошибка в update_sessions: {e}")

@tasks.loop(minutes=5)
async def update_health():
    if HEALTH_URL:
        try:
            async with aiohttp.ClientSession() as session:
                await session.get(HEALTH_URL, timeout=10)
            logger.info("✅ HEALTH URL pinged")
        except Exception as e:
            logger.warning(f"Health ping failed: {e}")

# ===== Bot events =====

@bot.event
async def on_ready():
    logger.info(f"✅ Bot started as {bot.user}")
    update_prices.start()
    update_volumes.start()
    update_fng.start()
    update_sessions.start()
    update_health.start()

# ===== Run bot =====
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN не установлен в переменных окружения")
        exit(1)
    bot.run(DISCORD_TOKEN)

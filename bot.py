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

# ===== Shared state for last values to avoid redundant updates =====
last_values = {
    "btc_price": None,
    "eth_price": None,
    "btc_vol": None,
    "eth_vol": None,
    "fng": None,
    "sessions_msg_id": None,
    "sessions_msg_content": None,
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

# ===== Market sessions handling =====

# Майами таймзона для расчётов
MIAMI_TZ = pytz.timezone("America/New_York")

def get_market_status(market_name, now_miami):
    """
    Рассчитывает статус рынка (open/closed) и время до закрытия или открытия.
    Учитывает выходные:
      - Рынки закрыты с пятницы 17:00 до воскресенья 17:00 (по Майами).
      - Рынки открыты в другое время.
    """
    # Ближайшая пятница 17:00
    days_until_friday = (4 - now_miami.weekday()) % 7
    this_friday_17 = (now_miami + timedelta(days=days_until_friday)).replace(hour=17, minute=0, second=0, microsecond=0)

    # Ближайшее воскресенье 17:00
    days_until_sunday = (6 - now_miami.weekday()) % 7
    this_sunday_17 = (now_miami + timedelta(days=days_until_sunday)).replace(hour=17, minute=0, second=0, microsecond=0)

    # Выходные: пятница 17:00 <= now < воскресенье 17:00
    if this_friday_17 <= now_miami < this_sunday_17:
        status = "closed"
        time_to_open = this_sunday_17 - now_miami
        return status, time_to_open

    # В остальное время — открыто, время до пятницы 17:00
    # Если сегодня пятница и после 17:00, то считаем пятницу следующей недели
    if now_miami >= this_friday_17:
        this_friday_17 += timedelta(days=7)

    status = "open"
    time_to_close = this_friday_17 - now_miami
    return status, time_to_close

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
        # Если открытие скоро — меньше часа, желтый
        if relative_seconds <= 3600:
            return "🟡"
        return "🔴"
    return ""

def format_updated_since(last_update_dt, now_dt):
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
        status, delta = get_market_status(market, now_miami)
        sessions_info[market] = {
            "status": status,
            "relative": int(delta.total_seconds()),
            "formatted_delta": format_timedelta(delta),
        }

    updated_text = format_updated_since(last_values.get("sessions_last_update", now_utc), now_utc)
    header = f"🕒 Market sessions (relative times, UTC) — {updated_text}\n\n"

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
    if channel is None:
        logger.warning("Sessions channel not found")
        return

    try:
        # Проверяем есть ли сохранённое сообщение сессий
        msg_id = last_values.get("sessions_msg_id")
        msg = None
        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
            except discord.NotFound:
                msg = None
        if msg is None:
            # Отправляем новое сообщение
            msg = await channel.send(message)
            last_values["sessions_msg_id"] = msg.id
            last_values["sessions_msg_content"] = message
            last_values["sessions_last_update"] = now_utc
            logger.info("Sessions message sent (new)")
        else:
            # Обновляем сообщение если содержимое изменилось
            if message != last_values.get("sessions_msg_content"):
                await msg.edit(content=message)
                last_values["sessions_msg_content"] = message
                last_values["sessions_last_update"] = now_utc
                logger.info("Sessions message updated")
    except Exception as e:
        logger.error(f"Failed to update sessions message: {e}")

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

@tasks.loop(minutes=17)
async def update_sessions():
    try:
        await update_sessions_message()
    except Exception as e:
        logger.error(f"Error in update_sessions task: {e}")

@tasks.loop(minutes=30)
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

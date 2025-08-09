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

# ===== Timezone =====
MIAMI_TZ = pytz.timezone("America/New_York")

# ===== Sessions logic =====
def get_next_open_datetime(now, weekday_target, hour, minute=0):
    """
    now — datetime по Майами
    weekday_target — 0 (понедельник) ... 6 (воскресенье)
    hour, minute — время открытия
    Возвращает ближайшую дату и время следующего открытия сессии.
    """
    days_ahead = (weekday_target - now.weekday()) % 7
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate

def format_timedelta(delta):
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

def get_sessions_status(now_utc):
    now_miami = now_utc.astimezone(MIAMI_TZ)

    # Сессии открытия: (weekday, hour)
    sessions = {
        "Tokyo": (6, 17),       # Воскресенье, 17:00
        "London": (0, 3),       # Понедельник, 03:00
        "New York": (0, 8),     # Понедельник, 08:00
    }

    result = {}
    for name, (wd, hr) in sessions.items():
        next_open = get_next_open_datetime(now_miami, wd, hr)
        delta = next_open - now_miami

        # Определяем статус:
        # Если сейчас между открытием и закрытием - считаем "open"
        # Предполагаем, что сессии открыты 24 часа до следующего открытия (простое приближение),
        # Можно расширить логику, если нужно точнее закрытие

        # Для точного закрытия можно задать время закрытия, например:
        # Tokyo: закрывается в пятницу 17:00 Майами
        # London: закрывается в пятницу 17:00 Майами
        # New York: закрывается в пятницу 17:00 Майами
        # Для простоты считаем, что сессия открыта с открытия до пятницы 17:00,
        # если сейчас после пятницы 17:00 — закрыта.

        # Рассчитаем ближайшее пятничное 17:00
        days_until_friday = (4 - now_miami.weekday()) % 7
        friday_17 = now_miami.replace(hour=17, minute=0, second=0, microsecond=0) + timedelta(days=days_until_friday)

        is_open = False
        # Сессия открыта если сейчас >= последнего открытия и < пятницы 17:00
        # Но если сейчас пятница после 17:00, считаем закрыто
        if now_miami >= next_open and now_miami < friday_17:
            is_open = True
        elif now_miami.weekday() == 4 and now_miami.hour >= 17:
            is_open = False

        if is_open:
            # Считаем время до закрытия пятницы 17:00
            time_to_close = friday_17 - now_miami
            result[name] = {
                "status": "open",
                "relative_seconds": int(time_to_close.total_seconds()),
                "formatted_delta": format_timedelta(time_to_close),
            }
        else:
            # Время до открытия
            result[name] = {
                "status": "closed",
                "relative_seconds": int(delta.total_seconds()),
                "formatted_delta": format_timedelta(delta),
            }

    return result

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
        return f"обновлено {mins} мин назад"
    else:
        hours = int(seconds // 3600)
        return f"обновлено {hours} ч назад"

async def update_sessions_message():
    now_utc = datetime.now(timezone.utc)
    sessions_info = get_sessions_status(now_utc)

    updated_text = format_updated_since(last_values.get("sessions_last_update"), now_utc)
    header = f"🕒 Market sessions (relative times, UTC) — {updated_text}\n\n"

    lines = []
    for market, info in sessions_info.items():
        emoji = get_session_status_emoji(info["status"], info["relative_seconds"])
        status_text = "open — closes in" if info["status"] == "open" else "closed — opens in"
        line = f"{emoji} {market}: {status_text} {info['formatted_delta']}"
        lines.append(line)

    footer = "\n\n⚠️ Countdown is relative (D days Hh Mm). Gap alerts posted for session opens."
    content = header + "\n".join(lines) + footer

    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not channel:
        logger.error("Sessions channel not found")
        return

    # Если сообщение уже создано — редактируем, иначе создаём новое
    if last_values.get("sessions_msg_id"):
        try:
            msg = await channel.fetch_message(last_values["sessions_msg_id"])
            if msg.content != content:
                await msg.edit(content=content)
                last_values["sessions_msg_content"] = content
                last_values["sessions_last_update"] = now_utc
                logger.info("Обновлено сообщение сессий")
        except (discord.NotFound, discord.Forbidden):
            # Сообщение удалено или недоступно, создаём заново
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

# ===== Main background task =====
@tasks.loop(seconds=30)
async def update_data_loop():
    async with aiohttp.ClientSession() as session:
        btc_price, btc_vol = await get_price_and_volume(session, "bitcoin")
        eth_price, eth_vol = await get_price_and_volume(session, "ethereum")
        fng = await get_fear_and_greed(session)

        if btc_price is not None:
            await update_channel_if_changed(BTC_PRICE_CHANNEL_ID, f"BTC: ${btc_price:,.2f}", "btc_price")
        if eth_price is not None:
            await update_channel_if_changed(ETH_PRICE_CHANNEL_ID, f"ETH: ${eth_price:,.2f}", "eth_price")
        if btc_vol is not None:
            await update_channel_if_changed(BTC_VOL_CHANNEL_ID, f"BTC Vol: {format_volume(btc_vol)}", "btc_vol")
        if eth_vol is not None:
            await update_channel_if_changed(ETH_VOL_CHANNEL_ID, f"ETH Vol: {format_volume(eth_vol)}", "eth_vol")
        if fng is not None:
            await update_channel_if_changed(FNG_CHANNEL_ID, f"Fear & Greed: {fng}", "fng")

    await update_sessions_message()

@update_data_loop.before_loop
async def before_update():
    await bot.wait_until_ready()
    logger.info("Bot is ready, starting update loop")

@bot.event
async def on_ready():
    logger.info(f"✅ Bot started as {bot.user}")
    if not update_data_loop.is_running():
        update_data_loop.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set")
        exit(1)
    bot.run(DISCORD_TOKEN)

import os
import logging
import asyncio
import aiohttp
import discord
from discord.ext import tasks, commands
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============== CONFIG ===============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_PRICE_CHANNEL_ID = int(os.getenv("BTC_PRICE_CHANNEL_ID"))
ETH_PRICE_CHANNEL_ID = int(os.getenv("ETH_PRICE_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
BTC_VOL_CHANNEL_ID = int(os.getenv("BTC_VOL_CHANNEL_ID"))
ETH_VOL_CHANNEL_ID = int(os.getenv("ETH_VOL_CHANNEL_ID"))
SESSIONS_CHANNEL_ID = int(os.getenv("SESSIONS_CHANNEL_ID"))  # Канал для сообщений с сессиями
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
    "sessions_last_update": None,
}

# ===== Async fetch with retry =====
async def fetch_json(session, url, max_retries=5):
    backoff = 1
    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", backoff))
                    logger.warning(f"429 rate limited, sleeping {retry_after} sec")
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

def format_updated_since(last_update_dt, now_dt=None):
    if now_dt is None:
        now_dt = datetime.utcnow()
    diff = now_dt - last_update_dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "обновлено только что"
    minutes = seconds // 60
    if minutes == 1:
        return "обновлено 1 мин назад"
    return f"обновлено {minutes} мин назад"

def get_session_status_emoji(status, relative_seconds):
    # status: 'open' or 'closed'
    # relative_seconds: время в секундах до открытия (если closed) или до закрытия (если open)
    # Если сессия открыта — 🟢
    # Если закрыта и время до открытия менее 1 часа — 🟡
    # Иначе 🔴
    if status == 'open':
        return "🟢"
    elif status == 'closed':
        if 0 < relative_seconds <= 3600:
            return "🟡"
        else:
            return "🔴"
    else:
        return ""

def format_relative_time(delta: timedelta):
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

# ===== Market sessions info =====
# UTC время открытия и закрытия
MARKET_SESSIONS = {
    "Tokyo": {"open": 0, "close": 9*3600},      # 00:00-09:00 UTC
    "London": {"open": 8*3600, "close": 17*3600},  # 08:00-17:00 UTC
    "New York": {"open": 13*3600, "close": 22*3600}, # 13:00-22:00 UTC
}

def get_next_weekday(dt, weekday):
    # weekday: 0=Monday, 6=Sunday
    days_ahead = weekday - dt.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return dt + timedelta(days=days_ahead)

def get_session_status(now_utc, session_name):
    session = MARKET_SESSIONS[session_name]
    open_ts = session["open"]
    close_ts = session["close"]
    now_seconds = now_utc.hour*3600 + now_utc.minute*60 + now_utc.second

    # Выходные: сессии не работают в субботу и воскресенье, считаем все закрытыми
    if now_utc.weekday() in (5,6):  # Sat=5, Sun=6
        # На выходных считаем ближайшее открытие следующим понедельником
        next_open_dt = datetime(now_utc.year, now_utc.month, now_utc.day) + timedelta(days=(7 - now_utc.weekday()))
        next_open_dt = next_open_dt.replace(hour=open_ts // 3600, minute=(open_ts % 3600)//60, second=0, microsecond=0)
        delta_to_open = next_open_dt - now_utc
        return {
            "status": "closed",
            "relative": delta_to_open,
            "relative_seconds": int(delta_to_open.total_seconds())
        }

    if open_ts <= now_seconds < close_ts:
        # Сессия открыта
        delta_to_close = timedelta(seconds=close_ts - now_seconds)
        return {
            "status": "open",
            "relative": delta_to_close,
            "relative_seconds": close_ts - now_seconds
        }
    else:
        # Сессия закрыта
        # Определим время открытия — сегодня или завтра
        if now_seconds < open_ts:
            next_open_dt = datetime(now_utc.year, now_utc.month, now_utc.day) + timedelta(seconds=open_ts - now_seconds)
        else:
            # После закрытия, открывается завтра
            next_day = now_utc + timedelta(days=1)
            next_open_dt = datetime(next_day.year, next_day.month, next_day.day) + timedelta(seconds=open_ts)
        delta_to_open = next_open_dt - now_utc
        return {
            "status": "closed",
            "relative": delta_to_open,
            "relative_seconds": int(delta_to_open.total_seconds())
        }

async def update_sessions_message():
    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not channel:
        logger.warning("Канал для сессий не найден")
        return

    now = datetime.utcnow().replace(second=0, microsecond=0)
    last_update = last_values.get("sessions_last_update")
    last_values["sessions_last_update"] = now

    # Формат времени обновления
    updated_text = format_updated_since(last_update, now) if last_update else f"обновлено {now.strftime('%Y-%m-%d %H:%M UTC')}"

    lines = [f"🕒 Рыночные сессии — {updated_text}", ""]

    for name in ["Tokyo", "London", "New York"]:
        info = get_session_status(now, name)
        emoji = get_session_status_emoji(info['status'], info['relative_seconds'])
        rel_time_str = format_relative_time(info['relative'])
        line = f"{emoji} {name}: {info['status']} — {'opens in' if info['status']=='closed' else 'closes in'} {rel_time_str}"
        lines.append(line)

    lines.append("")
    lines.append("⚠️ Countdown is relative (D days Hh Mm). Gap alerts posted for session opens.")

    msg_content = "\n".join(lines)

    # Пиннинг: либо редактировать пин или последнее закреплённое сообщение в канале
    pinned = await channel.pins()
    msg = None
    if pinned:
        msg = pinned[0]
        await msg.edit(content=msg_content)
    else:
        msg = await channel.send(msg_content)
        await msg.pin()

    logger.info(f"Updated sessions message in channel {SESSIONS_CHANNEL_ID}")

# ===== Update channel if changed =====
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

@tasks.loop(minutes=5)
async def update_sessions():
    await update_sessions_message()

@tasks.loop(minutes=9)
async def ping_health():
    if HEALTH_URL:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(HEALTH_URL, timeout=5) as resp:
                    if resp.status == 200:
                        logger.info("✅ HEALTH URL pinged")
                    else:
                        logger.warning(f"⚠️ HEALTH ping returned status {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка пинга HEALTH URL: {e}")

# ===== Bot event =====
@bot.event
async def on_ready():
    logger.info(f"✅ Bot started as {bot.user}")
    update_prices.start()
    update_volumes.start()
    update_fng.start()
    update_sessions.start()
    ping_health.start()

# ===== Run bot =====
bot.run(DISCORD_TOKEN)

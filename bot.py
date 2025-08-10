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

# ===== Time zone and sessions (corrected) =====
MIAMI_TZ = pytz.timezone("America/New_York")

# Реальные интервалы (время — в майамском поясе)
# Tokyo: Sun 17:00 (Miami) — Mon 02:00 (Miami)
# London: Mon 03:00 (Miami) — Mon 12:00 (Miami)
# New York: Mon 08:00 (Miami) — Mon 17:00 (Miami)
SESSIONS = {
    "Tokyo":    {"open_weekday": 6, "open_hour": 17, "close_weekday": 0, "close_hour": 2},
    "London":   {"open_weekday": 0, "open_hour": 3,  "close_weekday": 0, "close_hour": 12},
    "New York": {"open_weekday": 0, "open_hour": 8,  "close_weekday": 0, "close_hour": 17},
}

def _build_dt(base_dt, hour):
    """Helper: replace hour/min/sec preserving tzinfo"""
    return base_dt.replace(hour=hour, minute=0, second=0, microsecond=0)

def compute_candidate_open_close(now_miami, market):
    """
    Возвращает кортеж (candidate_open, candidate_close) для ближайшего открытия этой недели (может быть в прошлом/будущем).
    """
    cfg = SESSIONS[market]
    open_w, open_h = cfg["open_weekday"], cfg["open_hour"]
    close_w, close_h = cfg["close_weekday"], cfg["close_hour"]

    # candidate open (this week's occurrence)
    days_to_candidate = (open_w - now_miami.weekday()) % 7
    candidate_open = (now_miami + timedelta(days=days_to_candidate)).replace(hour=open_h, minute=0, second=0, microsecond=0)

    # candidate close relative to candidate_open
    days_from_open_to_close = (close_w - candidate_open.weekday()) % 7
    candidate_close = candidate_open.replace(hour=close_h, minute=0, second=0, microsecond=0) + timedelta(days=days_from_open_to_close)

    # Safety: ensure close is after open (if config is weird)
    if candidate_close <= candidate_open:
        candidate_close += timedelta(days=1)

    return candidate_open, candidate_close

def get_session_state(now_utc):
    """
    Возвращает dict: { market: { status, relative_seconds, formatted_delta, next_open, open_time, close_time } }
    """
    now_miami = now_utc.astimezone(MIAMI_TZ).replace(second=0, microsecond=0)
    out = {}

    for market in SESSIONS.keys():
        candidate_open, candidate_close = compute_candidate_open_close(now_miami, market)
        prev_open = candidate_open - timedelta(days=7)
        # compute prev_close from prev_open
        cfg = SESSIONS[market]
        close_w, close_h = cfg["close_weekday"], cfg["close_hour"]
        days_from_prev_open_to_close = (close_w - prev_open.weekday()) % 7
        prev_close = prev_open.replace(hour=close_h, minute=0, second=0, microsecond=0) + timedelta(days=days_from_prev_open_to_close)
        if prev_close <= prev_open:
            prev_close += timedelta(days=1)

        # Case 1: we're inside previous occurrence (this handles e.g. Mon 01:30 for Tokyo)
        if prev_open <= now_miami < prev_close:
            status = "open"
            delta = prev_close - now_miami
            open_time = prev_open
            close_time = prev_close
            next_open = candidate_open  # next occurrence is candidate_open
        # Case 2: we're inside candidate occurrence (rare when candidate_open is same-week and in past)
        elif candidate_open <= now_miami < candidate_close:
            status = "open"
            delta = candidate_close - now_miami
            open_time = candidate_open
            close_time = candidate_close
            next_open = candidate_open + timedelta(days=7)
        else:
            # closed: find nearest future open
            if now_miami < candidate_open:
                next_open = candidate_open
            else:
                next_open = candidate_open + timedelta(days=7)
            status = "closed"
            delta = next_open - now_miami
            open_time = None
            close_time = None

        out[market] = {
            "status": status,
            "relative_seconds": int(delta.total_seconds()),
            "formatted_delta": format_timedelta(delta),
            "next_open": next_open,
            "open_time": open_time,
            "close_time": close_time,
        }

    return out

def format_timedelta(delta):
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
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
        return "🟡" if relative_seconds <= 3600 else "🔴"
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
    sessions = get_session_state(now_utc)

    # Сортировка: открытые вверх, затем по времени до события (меньше первым)
    sorted_items = sorted(
        sessions.items(),
        key=lambda kv: (0 if kv[1]["status"] == "open" else 1, kv[1]["relative_seconds"])
    )

    updated_text = format_updated_since(last_values.get("sessions_last_update"), now_utc)
    header = f"🕒 Форекс сессии — {updated_text}\n\n"

    lines = []
    for market, info in sorted_items:
        emoji = get_session_status_emoji(info["status"], info["relative_seconds"])
        if info["status"] == "open":
            line = f"{emoji} {market}: открыта — закроется через {info['formatted_delta']}"
        else:
            line = f"{emoji} {market}: закрыта — откроется через {info['formatted_delta']}"
        lines.append(line)

    content = header + "\n".join(lines) + "\n\n"

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

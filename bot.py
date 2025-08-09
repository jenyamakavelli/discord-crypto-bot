import os
import logging
import asyncio
import aiohttp
import discord
import yfinance as yf
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
GAP_ALERTS_CHANNEL_ID = int(os.getenv("GAP_ALERTS_CHANNEL_ID"))
ECON_CALENDAR_CHANNEL_ID = int(os.getenv("ECON_CALENDAR_CHANNEL_ID"))
HEALTH_URL = os.getenv("HEALTH_URL")
# =====================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== Flask health server =====
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is running!"
Thread(target=lambda: app.run(host="0.0.0.0", port=8000), daemon=True).start()

# ===== Shared state =====
last_values = {
    "btc_price": None,
    "eth_price": None,
    "btc_vol": None,
    "eth_vol": None,
    "fng": None,
    "sessions_message_id": None,
    "gap_last_check": None,
}

# ===== Utils =====

def format_volume(vol):
    if vol >= 1_000_000_000:
        return f"${vol/1_000_000_000:.1f}B"
    elif vol >= 1_000_000:
        return f"${vol/1_000_000:.1f}M"
    else:
        return f"${vol:,.0f}"

def format_relative_time(dt):
    # Возвращает строку вида '1d 3h 45m' или '3h 12m' или '5m'
    parts = []
    days = dt.days
    seconds = dt.seconds
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

def get_session_status_emoji(status, rel_time_str):
    # status: "open", "closed"
    # rel_time_str - строка с временем "1d 3h 45m"
    if status == "open":
        return "🟢"
    elif status == "closed":
        # Если скоро открывается (менее 1 часа)
        # парсим rel_time_str на минуты
        total_minutes = 0
        for part in rel_time_str.split():
            if part.endswith("d"):
                total_minutes += int(part[:-1]) * 24 * 60
            elif part.endswith("h"):
                total_minutes += int(part[:-1]) * 60
            elif part.endswith("m"):
                total_minutes += int(part[:-1])
        if total_minutes <= 60:
            return "🟡"
        else:
            return "🔴"
    else:
        return ""

def time_since(dt: datetime):
    now = datetime.now(timezone.utc)
    diff = now - dt
    total_seconds = int(diff.total_seconds())
    if total_seconds < 60:
        return "обновлено только что"
    elif total_seconds < 3600:
        return f"обновлено {total_seconds // 60} мин назад"
    elif total_seconds < 86400:
        return f"обновлено {total_seconds // 3600} ч назад"
    else:
        return f"обновлено {total_seconds // 86400} д назад"

# ===== Async HTTP fetch =====
async def fetch_json(session, url, max_retries=5):
    backoff = 1
    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", backoff))
                    logger.warning(f"429 rate limited, sleeping {retry_after}s")
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

# ===== Price & volume =====
async def update_price_volume():
    async with aiohttp.ClientSession() as session:
        try:
            r_btc = await fetch_json(session, "https://api.coingecko.com/api/v3/coins/bitcoin")
            r_eth = await fetch_json(session, "https://api.coingecko.com/api/v3/coins/ethereum")
            if r_btc:
                btc_price = r_btc["market_data"]["current_price"]["usd"]
                btc_vol = r_btc["market_data"]["total_volume"]["usd"]
                if last_values["btc_price"] != btc_price:
                    channel = bot.get_channel(BTC_PRICE_CHANNEL_ID)
                    if channel:
                        await channel.edit(name=f"BTC: ${btc_price:,.2f}")
                        last_values["btc_price"] = btc_price
                if last_values["btc_vol"] != btc_vol:
                    channel = bot.get_channel(BTC_VOL_CHANNEL_ID)
                    if channel:
                        await channel.edit(name=f"BTC Vol: {format_volume(btc_vol)}")
                        last_values["btc_vol"] = btc_vol
            if r_eth:
                eth_price = r_eth["market_data"]["current_price"]["usd"]
                eth_vol = r_eth["market_data"]["total_volume"]["usd"]
                if last_values["eth_price"] != eth_price:
                    channel = bot.get_channel(ETH_PRICE_CHANNEL_ID)
                    if channel:
                        await channel.edit(name=f"ETH: ${eth_price:,.2f}")
                        last_values["eth_price"] = eth_price
                if last_values["eth_vol"] != eth_vol:
                    channel = bot.get_channel(ETH_VOL_CHANNEL_ID)
                    if channel:
                        await channel.edit(name=f"ETH Vol: {format_volume(eth_vol)}")
                        last_values["eth_vol"] = eth_vol
        except Exception as e:
            logger.error(f"Price/volume update error: {e}")

# ===== Fear & Greed =====
async def update_fng():
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(session, "https://api.alternative.me/fng/")
        if data:
            try:
                fng_val = int(data["data"][0]["value"])
                if last_values["fng"] != fng_val:
                    channel = bot.get_channel(FNG_CHANNEL_ID)
                    if channel:
                        await channel.edit(name=f"Fear & Greed: {fng_val}")
                        last_values["fng"] = fng_val
            except Exception as e:
                logger.warning(f"FNG parse error: {e}")

# ===== Market sessions countdown & status =====

MARKET_SESSIONS = {
    "Tokyo": {"open": 0, "close": 9},   # UTC hours
    "London": {"open": 8, "close": 17},
    "New York": {"open": 13, "close": 22},
}

async def update_sessions_message():
    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not channel:
        logger.warning("Sessions channel not found")
        return
    pinned = await channel.pins()
    pinned_message = None
    if pinned:
        pinned_message = pinned[0]
    now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    lines = []
    for session, hours in MARKET_SESSIONS.items():
        open_hour = hours["open"]
        close_hour = hours["close"]
        # Рассчитаем статус и относительное время
        # Считаем сейчас в UTC час
        current_hour = now_utc.hour
        # Состояния: open / closed
        if open_hour <= current_hour < close_hour:
            status = "open"
            # Время до закрытия
            close_time = now_utc.replace(hour=close_hour, minute=0)
            rel_delta = close_time - now_utc
            rel_str = format_relative_time(rel_delta)
            line = f"{session}: open — closes in {rel_str}"
        else:
            status = "closed"
            # Время до открытия
            if current_hour < open_hour:
                open_time = now_utc.replace(hour=open_hour, minute=0)
            else:
                open_time = (now_utc + timedelta(days=1)).replace(hour=open_hour, minute=0)
            rel_delta = open_time - now_utc
            rel_str = format_relative_time(rel_delta)
            line = f"{session}: closed — opens in {rel_str}"

        emoji = get_session_status_emoji(status, rel_str)
        lines.append(f"{emoji} {line}")

    # Формируем строку обновления
    updated_text = time_since(last_values.get("sessions_last_update", now_utc))

    content = (
        f"🕒 Market sessions (relative times, UTC) — {updated_text}\n\n"
        + "\n".join(lines)
        + "\n\n⚠️ Countdown is relative (D days Hh Mm). Gap alerts posted for session opens.\n"
    )

    if pinned_message:
        try:
            await pinned_message.edit(content=content)
        except Exception as e:
            logger.error(f"Failed to edit pinned sessions message: {e}")
    else:
        msg = await channel.send(content)
        await msg.pin()
    last_values["sessions_last_update"] = now_utc

# ===== Gap scanner =====

KEY_PAIRS = {
    "BTC-USD": {"symbol": "BTC-USD", "channel_id": GAP_ALERTS_CHANNEL_ID},
    "ETH-USD": {"symbol": "ETH-USD", "channel_id": GAP_ALERTS_CHANNEL_ID},
    "EURUSD=X": {"symbol": "EURUSD=X", "channel_id": GAP_ALERTS_CHANNEL_ID},
}

async def scan_gaps():
    now_utc = datetime.now(timezone.utc)
    for pair_name, info in KEY_PAIRS.items():
        try:
            ticker = yf.Ticker(info["symbol"])
            hist = ticker.history(period="2d", interval="1d")
            if len(hist) < 2:
                continue
            prev_close = hist["Close"].iloc[-2]
            open_price = hist["Open"].iloc[-1]
            gap = (open_price - prev_close) / prev_close
            gap_pct = gap * 100
            channel = bot.get_channel(info["channel_id"])

            if abs(gap_pct) >= 0.5:
                direction = "up" if gap_pct > 0 else "down"
                message = (
                    f"📊 Gap alert for {pair_name}:\n"
                    f"Previous close: ${prev_close:.2f}\n"
                    f"Today's open: ${open_price:.2f}\n"
                    f"Gap: {gap_pct:.2f}% {direction}\n"
                    f"Chances to close gap: ~{estimate_gap_closure_chance(gap_pct)}%"
                )
                if channel:
                    await channel.send(message)
        except Exception as e:
            logger.error(f"Gap scan error for {pair_name}: {e}")

def estimate_gap_closure_chance(gap_pct):
    # Простая эвристика — большие гэпы сложнее закрываются быстро
    gap_abs = abs(gap_pct)
    if gap_abs < 1:
        return 80
    elif gap_abs < 3:
        return 50
    elif gap_abs < 5:
        return 30
    else:
        return 10

# ===== Economic calendar (news) =====

ECON_NEWS_API_URL = "https://api.example.com/economic_calendar"  # заменить на реальный источник
LAST_NEWS_IDS = set()

async def update_economic_news():
    global LAST_NEWS_IDS
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(session, ECON_NEWS_API_URL)
        if not data or "events" not in data:
            return
        channel = bot.get_channel(ECON_CALENDAR_CHANNEL_ID)
        new_events = []
        for event in data["events"]:
            if event["id"] not in LAST_NEWS_IDS and event["impact"] == "high":
                new_events.append(event)
                LAST_NEWS_IDS.add(event["id"])

        for event in new_events:
            text = (
                f"🚨 High impact news incoming:\n"
                f"{event['time']} UTC - {event['country']} - {event['event']}\n"
                f"Forecast: {event['forecast']}\n"
                f"Previous: {event['previous']}"
            )
            if channel:
                await channel.send(text)

# ===== Background tasks =====

@tasks.loop(minutes=6)
async def prices_loop():
    await update_price_volume()

@tasks.loop(minutes=43)
async def fng_loop():
    await update_fng()

@tasks.loop(minutes=17)
async def volume_loop():
    await update_price_volume()  # объемы в том же запросе, можно оптимизировать

@tasks.loop(minutes=1)
async def sessions_loop():
    await update_sessions_message()

@tasks.loop(minutes=30)
async def gap_loop():
    await scan_gaps()

@tasks.loop(minutes=10)
async def econ_news_loop():
    await update_economic_news()

@bot.event
async def on_ready():
    logger.info(f"✅ Bot started as {bot.user}")
    prices_loop.start()
    fng_loop.start()
    volume_loop.start()
    sessions_loop.start()
    gap_loop.start()
    econ_news_loop.start()

# ===== Run =====

bot.run(DISCORD_TOKEN)

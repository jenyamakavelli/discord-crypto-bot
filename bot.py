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
logger = logging.getLogger("forex-bot")

# =============== CONFIG ===============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_PRICE_CHANNEL_ID = int(os.getenv("BTC_PRICE_CHANNEL_ID", "0"))
ETH_PRICE_CHANNEL_ID = int(os.getenv("ETH_PRICE_CHANNEL_ID", "0"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID", "0"))
BTC_VOL_CHANNEL_ID = int(os.getenv("BTC_VOL_CHANNEL_ID", "0"))
ETH_VOL_CHANNEL_ID = int(os.getenv("ETH_VOL_CHANNEL_ID", "0"))
SESSIONS_CHANNEL_ID = int(os.getenv("SESSIONS_CHANNEL_ID", "0"))
HEALTH_URL = os.getenv("HEALTH_URL")  # Для Koyeb Ping
# =====================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== Flask health server (для внешнего пинга) =====
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

last_price_messages = {
    "btc_price": None,
    "eth_price": None,
    "fng": None,
    "btc_vol": None,
    "eth_vol": None,
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
        mins = seconds // 60
        return f"{mins} мин назад"
    else:
        hours = seconds // 3600
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

# ===== Session definitions =====
SESSION_DEFS = {
    "Pacific": {"open_hour": 17, "open_minute": 0, "duration_hours": 9.0, "symbols": ["AUD", "NZD"]},
    "Tokyo": {"open_hour": 19, "open_minute": 0, "duration_hours": 9.0, "symbols": ["JPY", "CNY", "SGD", "HKD"]},
    "European": {"open_hour": 3, "open_minute": 0, "duration_hours": 9.0, "symbols": ["EUR", "GBP", "CHF"]},
    "American": {"open_hour": 8, "open_minute": 30, "duration_hours": 8.5, "symbols": ["USD"]},
}

def get_last_open_close(now_miami, open_hour, open_minute, duration_hours):
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
                next_open = last_open + timedelta(days=1)
                delta = next_open - now_miami
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
    footer_lines = ["\n───────────────\n", "📊 Время сессий (открытие — закрытие):\n"]
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

async def fetch_json(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=15) as resp:
            resp.raise_for_status()
            return await resp.json()

async def update_btc_price():
    if BTC_PRICE_CHANNEL_ID == 0:
        return
    try:
        data = await fetch_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
        price = data.get("bitcoin", {}).get("usd")
        if price is None:
            logger.warning("BTC price not found in API response")
            return
        content = f"💰 BTC цена: ${price:,.2f} USD"
        channel = bot.get_channel(BTC_PRICE_CHANNEL_ID)
        if not channel:
            logger.error("BTC price channel not found")
            return
        last_msg_id = last_price_messages["btc_price"]
        if last_msg_id:
            try:
                msg = await channel.fetch_message(last_msg_id)
                if msg.content != content:
                    await msg.edit(content=content)
                    logger.info("BTC price message updated")
            except (discord.NotFound, discord.Forbidden):
                msg = await channel.send(content)
                last_price_messages["btc_price"] = msg.id
                logger.info("BTC price message created")
        else:
            msg = await channel.send(content)
            last_price_messages["btc_price"] = msg.id
            logger.info("BTC price message created")
    except Exception as e:
        logger.error(f"Error updating BTC price: {e}")

async def update_eth_price():
    if ETH_PRICE_CHANNEL_ID == 0:
        return
    try:
        data = await fetch_json("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd")
        price = data.get("ethereum", {}).get("usd")
        if price is None:
            logger.warning("ETH price not found in API response")
            return
        content = f"💰 ETH цена: ${price:,.2f} USD"
        channel = bot.get_channel(ETH_PRICE_CHANNEL_ID)
        if not channel:
            logger.error("ETH price channel not found")
            return
        last_msg_id = last_price_messages["eth_price"]
        if last_msg_id:
            try:
                msg = await channel.fetch_message(last_msg_id)
                if msg.content != content:
                    await msg.edit(content=content)
                    logger.info("ETH price message updated")
            except (discord.NotFound, discord.Forbidden):
                msg = await channel.send(content)
                last_price_messages["eth_price"] = msg.id
                logger.info("ETH price message created")
        else:
            msg = await channel.send(content)
            last_price_messages["eth_price"] = msg.id
            logger.info("ETH price message created")
    except Exception as e:
        logger.error(f"Error updating ETH price: {e}")

async def update_fng():
    if FNG_CHANNEL_ID == 0:
        return
    try:
        # Fear & Greed Index API — https://api.alternative.me/fng/?limit=1
        data = await fetch_json("https://api.alternative.me/fng/?limit=1")
        value = data.get("data", [{}])[0].get("value")
        classification = data.get("data", [{}])[0].get("value_classification")
        if value is None or classification is None:
            logger.warning("FNG data missing")
            return
        content = f"📈 Fear & Greed Index: {value} ({classification})"
        channel = bot.get_channel(FNG_CHANNEL_ID)
        if not channel:
            logger.error("FNG channel not found")
            return
        last_msg_id = last_price_messages["fng"]
        if last_msg_id:
            try:
                msg = await channel.fetch_message(last_msg_id)
                if msg.content != content:
                    await msg.edit(content=content)
                    logger.info("FNG message updated")
            except (discord.NotFound, discord.Forbidden):
                msg = await channel.send(content)
                last_price_messages["fng"] = msg.id
                logger.info("FNG message created")
        else:
            msg = await channel.send(content)
            last_price_messages["fng"] = msg.id
            logger.info("FNG message created")
    except Exception as e:
        logger.error(f"Error updating FNG: {e}")

async def update_btc_vol():
    if BTC_VOL_CHANNEL_ID == 0:
        return
    try:
        data = await fetch_json("https://api.coingecko.com/api/v3/coins/bitcoin")
        vol = data.get("market_data", {}).get("total_volume", {}).get("usd")
        if vol is None:
            logger.warning("BTC volume not found")
            return
        vol_formatted = f"${vol:,.0f}"
        content = f"📊 BTC объем торгов (24ч): {vol_formatted}"
        channel = bot.get_channel(BTC_VOL_CHANNEL_ID)
        if not channel:
            logger.error("BTC vol channel not found")
            return
        last_msg_id = last_price_messages["btc_vol"]
        if last_msg_id:
            try:
                msg = await channel.fetch_message(last_msg_id)
                if msg.content != content:
                    await msg.edit(content=content)
                    logger.info("BTC vol message updated")
            except (discord.NotFound, discord.Forbidden):
                msg = await channel.send(content)
                last_price_messages["btc_vol"] = msg.id
                logger.info("BTC vol message created")
        else:
            msg = await channel.send(content)
            last_price_messages["btc_vol"] = msg.id
            logger.info("BTC vol message created")
    except Exception as e:
        logger.error(f"Error updating BTC vol: {e}")

async def update_eth_vol():
    if ETH_VOL_CHANNEL_ID == 0:
        return
    try:
        data = await fetch_json("https://api.coingecko.com/api/v3/coins/ethereum")
        vol = data.get("market_data", {}).get("total_volume", {}).get("usd")
        if vol is None:
            logger.warning("ETH volume not found")
            return
        vol_formatted = f"${vol:,.0f}"
        content = f"📊 ETH объем торгов (24ч): {vol_formatted}"
        channel = bot.get_channel(ETH_VOL_CHANNEL_ID)
        if not channel:
            logger.error("ETH vol channel not found")
            return
        last_msg_id = last_price_messages["eth_vol"]
        if last_msg_id:
            try:
                msg = await channel.fetch_message(last_msg_id)
                if msg.content != content:
                    await msg.edit(content=content)
                    logger.info("ETH vol message updated")
            except (discord.NotFound, discord.Forbidden):
                msg = await channel.send(content)
                last_price_messages["eth_vol"] = msg.id
                logger.info("ETH vol message created")
        else:
            msg = await channel.send(content)
            last_price_messages["eth_vol"] = msg.id
            logger.info("ETH vol message created")
    except Exception as e:
        logger.error(f"Error updating ETH vol: {e}")

# ===== Background tasks =====
@tasks.loop(seconds=60)
async def update_sessions():
    try:
        await update_sessions_message()
    except Exception as e:
        logger.exception("Error in update_sessions task: %s", e)

@tasks.loop(minutes=6)
async def update_btc_eth_prices_loop():
    try:
        await update_btc_price()
        await update_eth_price()
    except Exception as e:
        logger.exception(f"Error in BTC/ETH price update: {e}")

@tasks.loop(minutes=45)
async def update_fng_loop():
    try:
        await update_fng()
    except Exception as e:
        logger.exception(f"Error in FNG update: {e}")

@tasks.loop(minutes=30)
async def update_volumes_loop():
    try:
        await update_btc_vol()
        await update_eth_vol()
    except Exception as e:
        logger.exception(f"Error in volume update: {e}")

@tasks.loop(minutes=5)
async def ping_health():
    if not HEALTH_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(HEALTH_URL, timeout=10) as resp:
                logger.debug("Pinged health URL %s status=%s", HEALTH_URL, resp.status)
    except Exception as e:
        logger.warning("Health ping failed: %s", e)

# ===== Startup =====
@bot.event
async def on_ready():
    logger.info("Bot started as %s", bot.user)
    if not update_sessions.is_running():
        update_sessions.start()
    if not update_btc_eth_prices_loop.is_running():
        update_btc_eth_prices_loop.start()
    if not update_fng_loop.is_running():
        update_fng_loop.start()
    if not update_volumes_loop.is_running():
        update_volumes_loop.start
    if HEALTH_URL and not ping_health.is_running():
        ping_health.start()
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN)

import os
import logging
import asyncio
import aiohttp
import discord
from discord.ext import tasks, commands
from flask import Flask
from threading import Thread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============== CONFIG ===============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_PRICE_CHANNEL_ID = int(os.getenv("BTC_PRICE_CHANNEL_ID"))
ETH_PRICE_CHANNEL_ID = int(os.getenv("ETH_PRICE_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
BTC_VOL_CHANNEL_ID = int(os.getenv("BTC_VOL_CHANNEL_ID"))
ETH_VOL_CHANNEL_ID = int(os.getenv("ETH_VOL_CHANNEL_ID"))
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

# ===== Tasks =====
@tasks.loop(minutes=3)
async def update_prices():
    async with aiohttp.ClientSession() as session:
        btc_price, _ = await get_price_and_volume(session, "bitcoin")
        eth_price, _ = await get_price_and_volume(session, "ethereum")
        if btc_price is not None:
            await update_channel_if_changed(BTC_PRICE_CHANNEL_ID, f"BTC: ${btc_price:,.2f}", "btc_price")
        if eth_price is not None:
            await update_channel_if_changed(ETH_PRICE_CHANNEL_ID, f"ETH: ${eth_price:,.2f}", "eth_price")

@tasks.loop(minutes=10)
async def update_volumes():
    async with aiohttp.ClientSession() as session:
        _, btc_vol = await get_price_and_volume(session, "bitcoin")
        _, eth_vol = await get_price_and_volume(session, "ethereum")
        if btc_vol is not None:
            await update_channel_if_changed(BTC_VOL_CHANNEL_ID, f"BTC Vol: {format_volume(btc_vol)}", "btc_vol")
        if eth_vol is not None:
            await update_channel_if_changed(ETH_VOL_CHANNEL_ID, f"ETH Vol: {format_volume(eth_vol)}", "eth_vol")

@tasks.loop(minutes=45)
async def update_fng():
    async with aiohttp.ClientSession() as session:
        fng_value = await get_fear_and_greed(session)
        if fng_value is not None:
            await update_channel_if_changed(FNG_CHANNEL_ID, f"Fear & Greed: {fng_value}", "fng")

@tasks.loop(minutes=10)
async def ping_health():
    if HEALTH_URL:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(HEALTH_URL, timeout=5) as resp:
                    if resp.status == 200:
                        logger.info("✅ HEALTH URL пингован")
                    else:
                        logger.warning(f"⚠️ HEALTH ping returned status {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка пинга HEALTH URL: {e}")

# ===== Bot event =====
@bot.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {bot.user}")
    update_prices.start()
    update_volumes.start()
    update_fng.start()
    ping_health.start()

# ===== Run bot =====
bot.run(DISCORD_TOKEN)

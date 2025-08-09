import os
import logging
import requests
import discord
from discord.ext import tasks, commands
from flask import Flask
from threading import Thread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== CONFIG ====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_PRICE_CHANNEL_ID = int(os.getenv("BTC_PRICE_CHANNEL_ID"))
ETH_PRICE_CHANNEL_ID = int(os.getenv("ETH_PRICE_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
BTC_VOL_CHANNEL_ID = int(os.getenv("BTC_VOL_CHANNEL_ID"))
ETH_VOL_CHANNEL_ID = int(os.getenv("ETH_VOL_CHANNEL_ID"))
HEALTH_URL = os.getenv("HEALTH_URL")  # Для Koyeb Ping
# =================================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== Flask health server =====
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=8000)

Thread(target=run_flask).start()

# ===== API функции =====
def get_price_and_volume(symbol_id):
    url = f"https://api.coingecko.com/api/v3/coins/{symbol_id}"
    r = requests.get(url)
    if r.status_code != 200:
        logger.warning(f"Ошибка CoinGecko для {symbol_id}: {r.status_code}")
        return None, None
    data = r.json()
    price = data["market_data"]["current_price"]["usd"]
    vol = data["market_data"]["total_volume"]["usd"]
    return price, vol

def get_fear_and_greed():
    url = "https://api.alternative.me/fng/"
    r = requests.get(url)
    if r.status_code != 200:
        logger.warning(f"Ошибка FNG API: {r.status_code}")
        return None
    return int(r.json()["data"][0]["value"])

def format_volume(vol):
    if vol >= 1_000_000_000:
        return f"${vol/1_000_000_000:.1f}B"
    elif vol >= 1_000_000:
        return f"${vol/1_000_000:.1f}M"
    else:
        return f"${vol:,.0f}"

# ===== Discord задачи =====
@tasks.loop(minutes=5)
async def update_prices():
    logger.info("🔄 Обновляю цены BTC и ETH...")
    btc_price, _ = get_price_and_volume("bitcoin")
    eth_price, _ = get_price_and_volume("ethereum")

    if btc_price:
        channel = bot.get_channel(BTC_PRICE_CHANNEL_ID)
        await channel.edit(name=f"BTC: ${btc_price:,.2f}")
    if eth_price:
        channel = bot.get_channel(ETH_PRICE_CHANNEL_ID)
        await channel.edit(name=f"ETH: ${eth_price:,.2f}")

@tasks.loop(minutes=15)
async def update_volumes():
    logger.info("🔄 Обновляю объёмы торгов...")
    _, btc_vol = get_price_and_volume("bitcoin")
    _, eth_vol = get_price_and_volume("ethereum")

    if btc_vol:
        channel = bot.get_channel(BTC_VOL_CHANNEL_ID)
        await channel.edit(name=f"BTC Vol: {format_volume(btc_vol)}")
    if eth_vol:
        channel = bot.get_channel(ETH_VOL_CHANNEL_ID)
        await channel.edit(name=f"ETH Vol: {format_volume(eth_vol)}")

@tasks.loop(minutes=30)
async def update_fng():
    logger.info("🔄 Обновляю индекс страха и жадности...")
    fng_value = get_fear_and_greed()
    if fng_value is not None:
        channel = bot.get_channel(FNG_CHANNEL_ID)
        await channel.edit(name=f"Fear & Greed: {fng_value}")

@tasks.loop(minutes=10)
async def ping_health():
    if HEALTH_URL:
        try:
            requests.get(HEALTH_URL)
            logger.info("✅ HEALTH URL пингован")
        except:
            logger.warning("⚠️ Ошибка пинга HEALTH URL")

@bot.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {bot.user}")
    update_prices.start()
    update_volumes.start()
    update_fng.start()
    ping_health.start()

bot.run(DISCORD_TOKEN)

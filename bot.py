import os
import asyncio
import logging
from flask import Flask
from threading import Thread
import aiohttp
import discord
from discord.ext import commands, tasks

# ==== ЛОГИ ====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ==== HTTP СЕРВЕР ДЛЯ KOYEB ====
app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.daemon = True
    t.start()

# ==== DISCORD БОТ ====
TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID", 0))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID", 0))
HEALTH_URL = os.getenv("HEALTH_URL")  # URL сервиса на Koyeb (для self-ping)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

last_prices = {"BTC": None, "ETH": None}

async def fetch_price(coin_id):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return float(data[coin_id]["usd"])

@tasks.loop(minutes=5)
async def update_prices():
    logger.info("🔄 Начинаю обновление цен")
    try:
        btc_price = await fetch_price("bitcoin")
        eth_price = await fetch_price("ethereum")
        logger.info(f"💰 BTC: {btc_price}, ETH: {eth_price}")

        last_prices["BTC"] = btc_price
        last_prices["ETH"] = eth_price

        btc_channel = bot.get_channel(BTC_CHANNEL_ID)
        eth_channel = bot.get_channel(ETH_CHANNEL_ID)

        if btc_channel:
            await btc_channel.edit(name=f"BTC: {btc_price:,.2f}$")
        if eth_channel:
            await eth_channel.edit(name=f"ETH: {eth_price:,.2f}$")

    except Exception as e:
        logger.error(f"Ошибка при обновлении цен: {e}")

@tasks.loop(minutes=4)
async def self_ping():
    if not HEALTH_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(HEALTH_URL) as resp:
                logger.info(f"Self-ping: {resp.status}")
    except Exception as e:
        logger.warning(f"Ошибка self-ping: {e}")

@bot.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {bot.user}")
    update_prices.start()
    self_ping.start()

if __name__ == "__main__":
    if not TOKEN or not BTC_CHANNEL_ID or not ETH_CHANNEL_ID:
        logger.error("❌ Нет переменных окружения DISCORD_TOKEN, BTC_CHANNEL_ID или ETH_CHANNEL_ID")
        exit(1)

    keep_alive()  # Запускаем веб-сервер для health-check
    bot.run(TOKEN)

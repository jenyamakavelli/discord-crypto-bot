import os
import asyncio
import logging
import requests
from flask import Flask
from threading import Thread
import discord
from discord.ext import tasks

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))
FEAR_GREED_CHANNEL_ID = int(os.getenv("FEAR_GREED_CHANNEL_ID"))
BTC_VOL_CHANNEL_ID = int(os.getenv("BTC_VOL_CHANNEL_ID"))
ETH_VOL_CHANNEL_ID = int(os.getenv("ETH_VOL_CHANNEL_ID"))

# HTTP server to keep Koyeb alive
app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

def run_flask():
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

# Start Flask in a separate thread
Thread(target=run_flask).start()

# Discord bot setup
intents = discord.Intents.default()
client = discord.Client(intents=intents)

# Format large numbers to short form (e.g. 1.2B, 500M)
def format_large_number(num):
    for unit in ["", "K", "M", "B", "T"]:
        if abs(num) < 1000:
            return f"{num:.1f}{unit}"
        num /= 1000
    return f"{num:.1f}P"

async def update_prices():
    logger.info("🔄 Обновляю цены BTC и ETH...")
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "bitcoin,ethereum", "vs_currencies": "usd"})
        data = r.json()
        btc_price = data["bitcoin"]["usd"]
        eth_price = data["ethereum"]["usd"]

        await client.get_channel(BTC_CHANNEL_ID).edit(name=f"BTC: ${btc_price:,.2f}")
        await client.get_channel(ETH_CHANNEL_ID).edit(name=f"ETH: ${eth_price:,.2f}")
        logger.info(f"Обновлены цены BTC и ETH: {btc_price}, {eth_price}")
    except Exception as e:
        logger.error(f"Ошибка при обновлении цен: {e}")

async def update_fear_greed():
    logger.info("🔄 Обновляю индекс страха и жадности...")
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1&format=json")
        value = r.json()["data"][0]["value"]
        await client.get_channel(FEAR_GREED_CHANNEL_ID).edit(name=f"Fear & Greed: {value}")
        logger.info(f"Обновлён индекс страха и жадности: {value}")
    except Exception as e:
        logger.error(f"Ошибка при обновлении индекса: {e}")

async def update_volumes():
    logger.info("🔄 Обновляю объёмы торгов...")
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                         params={"vs_currency": "usd", "ids": "bitcoin,ethereum"})
        data = {coin["id"]: coin["total_volume"] for coin in r.json()}

        btc_vol = format_large_number(data["bitcoin"])
        eth_vol = format_large_number(data["ethereum"])

        await client.get_channel(BTC_VOL_CHANNEL_ID).edit(name=f"BTC Vol: ${btc_vol}")
        await client.get_channel(ETH_VOL_CHANNEL_ID).edit(name=f"ETH Vol: ${eth_vol}")

        logger.info(f"Обновлены объёмы: BTC {btc_vol}, ETH {eth_vol}")
    except Exception as e:
        logger.error(f"Ошибка при обновлении объёмов: {e}")

@tasks.loop(minutes=5)
async def update_all():
    await update_prices()
    await update_fear_greed()
    await update_volumes()

@client.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {client.user}")
    update_all.start()

if __name__ == "__main__":
    client.run(TOKEN)

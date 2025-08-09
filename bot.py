import os
import asyncio
import logging
from discord.ext import commands, tasks
import discord
import requests
from flask import Flask
from threading import Thread

# Настройка логгера
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
VOLUME_CHANNEL_ID = int(os.getenv("VOLUME_CHANNEL_ID"))
HEALTH_URL = os.getenv("HEALTH_URL", "http://localhost:8000/health")

intents = discord.Intents.default()
client = commands.Bot(command_prefix="!", intents=intents)

app = Flask("bot")

@app.route("/health")
def health_check():
    return "OK", 200

def run_flask():
    app.run(host="0.0.0.0", port=8000)

def format_volume(vol: float) -> str:
    abs_vol = abs(vol)
    if abs_vol >= 1_000_000_000_000:
        return f"{vol / 1_000_000_000_000:.1f}T"
    elif abs_vol >= 1_000_000_000:
        return f"{vol / 1_000_000_000:.1f}B"
    elif abs_vol >= 1_000_000:
        return f"{vol / 1_000_000:.1f}M"
    elif abs_vol >= 1_000:
        return f"{vol / 1_000:.1f}K"
    else:
        return f"{vol:.0f}"

@client.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {client.user}")
    update_prices.start()
    update_fng.start()
    update_volume.start()

def get_coingecko_price_volume(ids: list):
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": ",".join(ids),
        "vs_currencies": "usd",
        "include_24hr_vol": "true",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"Ошибка запроса CoinGecko price/volume: {e}")
        return None

def get_fear_greed_index():
    url = "https://api.alternative.me/fng/"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "data" in data and len(data["data"]) > 0:
            value = data["data"][0].get("value")
            return int(value)
        else:
            logger.warning("FNG data format unexpected")
            return None
    except Exception as e:
        logger.warning(f"Ошибка запроса Fear & Greed Index: {e}")
        return None

@tasks.loop(minutes=3)
async def update_prices():
    logger.info("🔄 Обновляю цены BTC и ETH...")
    data = get_coingecko_price_volume(["bitcoin", "ethereum"])
    if not data:
        return

    btc_price = data.get("bitcoin", {}).get("usd")
    eth_price = data.get("ethereum", {}).get("usd")
    if btc_price is None or eth_price is None:
        logger.warning("Нет данных по ценам BTC или ETH")
        return

    btc_channel = client.get_channel(BTC_CHANNEL_ID)
    eth_channel = client.get_channel(ETH_CHANNEL_ID)
    if btc_channel:
        try:
            await btc_channel.edit(name=f"BTC: ${btc_price:,.0f}")
            logger.info(f"Обновлено имя канала BTC: ${btc_price:,.0f}")
        except discord.HTTPException as e:
            logger.warning(f"Ошибка обновления канала BTC: {e}")
    if eth_channel:
        try:
            await eth_channel.edit(name=f"ETH: ${eth_price:,.2f}")
            logger.info(f"Обновлено имя канала ETH: ${eth_price:,.2f}")
        except discord.HTTPException as e:
            logger.warning(f"Ошибка обновления канала ETH: {e}")

@tasks.loop(minutes=15)
async def update_fng():
    logger.info("🔄 Обновляю индекс страха и жадности...")
    fng_value = get_fear_greed_index()
    if fng_value is None:
        return
    fng_channel = client.get_channel(FNG_CHANNEL_ID)
    if fng_channel:
        try:
            await fng_channel.edit(name=f"Fear & Greed: {fng_value}")
            logger.info(f"Обновлено имя канала Fear & Greed: {fng_value}")
        except discord.HTTPException as e:
            logger.warning(f"Ошибка обновления канала Fear & Greed: {e}")

@tasks.loop(minutes=3)
async def update_volume():
    logger.info("🔄 Обновляю объёмы торгов...")
    data = get_coingecko_price_volume(["bitcoin", "ethereum"])
    if not data:
        return
    btc_vol = data.get("bitcoin", {}).get("usd_24h_vol")
    eth_vol = data.get("ethereum", {}).get("usd_24h_vol")
    vol_channel = client.get_channel(VOLUME_CHANNEL_ID)
    if vol_channel:
        parts = []
        if btc_vol is not None:
            parts.append(f"BTC Vol: ${format_volume(btc_vol)}")
        if eth_vol is not None:
            parts.append(f"ETH Vol: ${format_volume(eth_vol)}")
        vol_name = " | ".join(parts)
        try:
            await vol_channel.edit(name=vol_name)
            logger.info(f"Обновлено имя канала объёмов: {vol_name}")
        except discord.HTTPException as e:
            logger.warning(f"Ошибка обновления канала объёмов: {e}")

if __name__ == "__main__":
    Thread(target=run_flask).start()
    client.run(TOKEN)

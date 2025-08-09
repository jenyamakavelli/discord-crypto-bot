import os
import asyncio
import logging
import aiohttp
from discord.ext import commands, tasks
import discord

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("DISCORD_TOKEN")

BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
BINANCE_VOLUME_CHANNEL_ID = None  # убрали Binance, не нужен
COINGECKO_VOLUME_CHANNEL_ID = int(os.getenv("COINGECKO_VOLUME_CHANNEL_ID"))

intents = discord.Intents.default()
client = commands.Bot(command_prefix="!", intents=intents)

last_prices = {"btc": None, "eth": None}
last_fng = None
last_volumes = {"btc": None, "eth": None}

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; CryptoPriceBot/1.0)"
}

async def fetch_json(session, url, params=None):
    async with session.get(url, params=params, headers=HEADERS) as resp:
        resp.raise_for_status()
        return await resp.json()

@tasks.loop(minutes=3)
async def update_prices_and_volumes():
    global last_prices, last_volumes
    logging.info("🔄 Обновляю цены и объемы BTC и ETH с CoinGecko...")
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": "bitcoin,ethereum",
        "order": "market_cap_desc",
        "per_page": 2,
        "page": 1,
        "sparkline": "false"
    }
    try:
        async with aiohttp.ClientSession() as session:
            data = await fetch_json(session, url, params)
            btc_data = data[0]
            eth_data = data[1]

            btc_price = round(btc_data["current_price"], 2)
            eth_price = round(eth_data["current_price"], 2)
            btc_volume = int(btc_data["total_volume"])
            eth_volume = int(eth_data["total_volume"])

            # Обновление цен BTC
            if last_prices["btc"] != btc_price:
                channel = client.get_channel(BTC_CHANNEL_ID)
                if channel:
                    await channel.edit(name=f"BTC: ${btc_price:,}")
                    logging.info(f"Обновлено имя канала BTC: BTC: ${btc_price:,}")
                last_prices["btc"] = btc_price

            # Обновление цен ETH
            if last_prices["eth"] != eth_price:
                channel = client.get_channel(ETH_CHANNEL_ID)
                if channel:
                    await channel.edit(name=f"ETH: ${eth_price:,}")
                    logging.info(f"Обновлено имя канала ETH: ETH: ${eth_price:,}")
                last_prices["eth"] = eth_price

            # Обновление объемов BTC
            if last_volumes["btc"] != btc_volume:
                channel = client.get_channel(COINGECKO_VOLUME_CHANNEL_ID)
                if channel:
                    await channel.edit(name=f"BTC Vol: ${btc_volume:,}")
                    logging.info(f"Обновлено имя канала BTC Vol: ${btc_volume:,}")
                last_volumes["btc"] = btc_volume

            # Обновление объемов ETH
            if last_volumes["eth"] != eth_volume:
                # Для наглядности можно добавить канал для ETH объемов, если нужно
                # Или объединить с BTC объемом в один канал
                pass  # здесь пока не реализуем, чтобы не усложнять
                last_volumes["eth"] = eth_volume

    except Exception as e:
        logging.warning(f"Ошибка при обновлении цен и объемов: {e}")

@tasks.loop(minutes=30)
async def update_fear_and_greed():
    global last_fng
    logging.info("🔄 Обновляю индекс страха и жадности...")
    url = "https://api.alternative.me/fng/"
    try:
        async with aiohttp.ClientSession() as session:
            data = await fetch_json(session, url)
            fng_value = int(data["data"][0]["value"])
            if last_fng != fng_value:
                channel = client.get_channel(FNG_CHANNEL_ID)
                if channel:
                    await channel.edit(name=f"Fear & Greed: {fng_value}")
                    logging.info(f"Обновлено имя канала Fear & Greed: {fng_value}")
                last_fng = fng_value
    except Exception as e:
        logging.warning(f"Ошибка при обновлении индекса страха и жадности: {e}")

@client.event
async def on_ready():
    logging.info(f"✅ Бот запущен как {client.user}")
    update_prices_and_volumes.start()
    update_fear_and_greed.start()

if __name__ == "__main__":
    client.run(TOKEN)

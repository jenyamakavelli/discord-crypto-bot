import os
import asyncio
import aiohttp
import logging
from discord.ext import commands, tasks
import discord

logging.basicConfig(level=logging.INFO)

# ENV переменные
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
BINANCE_VOLUME_CHANNEL_ID = int(os.getenv("BINANCE_VOLUME_CHANNEL_ID"))
COINBASE_VOLUME_CHANNEL_ID = int(os.getenv("COINBASE_VOLUME_CHANNEL_ID"))

intents = discord.Intents.default()
client = commands.Bot(command_prefix="!", intents=intents)

# Для отслеживания последних значений
last_btc_price = 0
last_eth_price = 0
last_fng = ""
last_binance_volume = 0
last_coinbase_volume = 0

async def fetch_json(url, session):
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logging.warning(f"Ошибка запроса {url}: статус {resp.status}")
    except Exception as e:
        logging.warning(f"Ошибка запроса {url}: {e}")
    return None

async def update_channel_name(channel_id, new_name):
    channel = client.get_channel(channel_id)
    if channel and channel.name != new_name:
        try:
            await channel.edit(name=new_name)
            logging.info(f"Обновлено имя канала {channel_id}: {new_name}")
        except Exception as e:
            logging.warning(f"Ошибка обновления канала {channel_id}: {e}")

@tasks.loop(minutes=3)
async def update_prices():
    global last_btc_price, last_eth_price
    logging.info("🔄 Обновляю цены BTC и ETH...")
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd"
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(url, session)
        if data:
            btc_price = data.get("bitcoin", {}).get("usd", 0)
            eth_price = data.get("ethereum", {}).get("usd", 0)
            if btc_price != last_btc_price:
                await update_channel_name(BTC_CHANNEL_ID, f"BTC: ${btc_price:,.2f}")
                last_btc_price = btc_price
            if eth_price != last_eth_price:
                await update_channel_name(ETH_CHANNEL_ID, f"ETH: ${eth_price:,.2f}")
                last_eth_price = eth_price

@tasks.loop(minutes=15)
async def update_fng():
    global last_fng
    logging.info("🔄 Обновляю индекс страха и жадности...")
    url = "https://api.alternative.me/fng/?limit=1"
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(url, session)
        if data and "data" in data and len(data["data"]) > 0:
            fng_value = data["data"][0].get("value", "")
            fng_str = f"Fear & Greed: {fng_value}"
            if fng_str != last_fng:
                await update_channel_name(FNG_CHANNEL_ID, fng_str)
                last_fng = fng_str

@tasks.loop(minutes=5)
async def update_binance_volume():
    global last_binance_volume
    logging.info("🔄 Обновляю объёмы Binance...")
    url = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(url, session)
        if data:
            volume = float(data.get("quoteVolume", 0))
            vol_str = f"Binance Vol: {volume:,.0f}"
            if vol_str != last_binance_volume:
                await update_channel_name(BINANCE_VOLUME_CHANNEL_ID, vol_str)
                last_binance_volume = vol_str

@tasks.loop(minutes=5)
async def update_coinbase_volume():
    global last_coinbase_volume
    logging.info("🔄 Обновляю объёмы Coinbase...")
    url = "https://api.pro.coinbase.com/products/BTC-USD/stats"
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(url, session)
        if data:
            volume = float(data.get("volume", 0))
            vol_str = f"Coinbase Vol: {volume:,.0f}"
            if vol_str != last_coinbase_volume:
                await update_channel_name(COINBASE_VOLUME_CHANNEL_ID, vol_str)
                last_coinbase_volume = vol_str

@client.event
async def on_ready():
    logging.info(f"✅ Бот запущен как {client.user}")
    update_prices.start()
    update_fng.start()
    update_binance_volume.start()
    update_coinbase_volume.start()

client.run(DISCORD_TOKEN)

import os
import asyncio
import aiohttp
import logging
from discord.ext import commands, tasks
import discord

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Конфиги из окружения ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
BTC_VOLUME_CHANNEL_ID = int(os.getenv("BTC_VOLUME_CHANNEL_ID"))
ETH_VOLUME_CHANNEL_ID = int(os.getenv("ETH_VOLUME_CHANNEL_ID"))
HEALTH_URL = os.getenv("HEALTH_URL")  # Для Koyeb ping

# --- Discord бот ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Хранилище последних значений для обновления только при изменении ---
last_prices = {}
last_fng = None
last_volumes = {"bitcoin": None, "ethereum": None}

# --- Универсальная функция запроса JSON с логированием и таймаутом ---
async def fetch_json(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logger.warning(f"Ошибка запроса {url}: статус {resp.status}")
                return None
    except Exception as e:
        logger.warning(f"Ошибка запроса {url}: {e}")
        return None

# --- Форматируем объёмы в читаемый вид, например 30.5B ---
def format_volume(volume):
    if volume is None:
        return "N/A"
    if volume >= 1e9:
        return f"{volume / 1e9:.1f}B"
    if volume >= 1e6:
        return f"{volume / 1e6:.1f}M"
    if volume >= 1e3:
        return f"{volume / 1e3:.1f}K"
    return str(round(volume, 2))

# --- Обновление цены BTC/ETH с CoinGecko ---
async def update_price(session, coin_id, channel_id):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    data = await fetch_json(session, url)
    if not data or coin_id not in data:
        return
    price = data[coin_id]["usd"]
    if last_prices.get(coin_id) != price:
        last_prices[coin_id] = price
        channel = bot.get_channel(channel_id)
        if channel:
            try:
                await channel.edit(name=f"{coin_id.upper()}: ${price:,.2f}")
                logger.info(f"Обновлено имя канала {channel_id}: {coin_id.upper()}: ${price:,.2f}")
            except Exception as e:
                logger.warning(f"Не удалось обновить канал {channel_id}: {e}")

# --- Обновление индекса страха и жадности (FNG) ---
async def update_fng(session):
    url = "https://api.alternative.me/fng/"
    data = await fetch_json(session, url)
    if not data or "data" not in data or len(data["data"]) == 0:
        return
    fng_value = data["data"][0]["value"]
    global last_fng
    if last_fng != fng_value:
        last_fng = fng_value
        channel = bot.get_channel(FNG_CHANNEL_ID)
        if channel:
            try:
                await channel.edit(name=f"Fear & Greed: {fng_value}")
                logger.info(f"Обновлено имя канала {FNG_CHANNEL_ID}: Fear & Greed: {fng_value}")
            except Exception as e:
                logger.warning(f"Не удалось обновить канал FNG: {e}")

# --- Обновление объёмов торгов BTC и ETH через CoinGecko ---
async def update_volumes(session):
    url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=bitcoin,ethereum"
    data = await fetch_json(session, url)
    if not data or len(data) < 2:
        return

    btc_volume = data[0].get("total_volume")
    eth_volume = data[1].get("total_volume")

    global last_volumes

    if last_volumes["bitcoin"] != btc_volume:
        last_volumes["bitcoin"] = btc_volume
        channel = bot.get_channel(BTC_VOLUME_CHANNEL_ID)
        if channel:
            try:
                await channel.edit(name=f"BTC Vol: ${format_volume(btc_volume)}")
                logger.info(f"Обновлено имя канала {BTC_VOLUME_CHANNEL_ID}: BTC Vol: ${format_volume(btc_volume)}")
            except Exception as e:
                logger.warning(f"Не удалось обновить канал объёмов BTC: {e}")

    if last_volumes["ethereum"] != eth_volume:
        last_volumes["ethereum"] = eth_volume
        channel = bot.get_channel(ETH_VOLUME_CHANNEL_ID)
        if channel:
            try:
                await channel.edit(name=f"ETH Vol: ${format_volume(eth_volume)}")
                logger.info(f"Обновлено имя канала {ETH_VOLUME_CHANNEL_ID}: ETH Vol: ${format_volume(eth_volume)}")
            except Exception as e:
                logger.warning(f"Не удалось обновить канал объёмов ETH: {e}")

# --- Задача пинга для Koyeb, чтобы не уходил в спячку ---
async def health_ping():
    if HEALTH_URL:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(HEALTH_URL, timeout=5) as resp:
                    if resp.status == 200:
                        logger.info("Health ping success")
                    else:
                        logger.warning(f"Health ping failed with status {resp.status}")
            except Exception as e:
                logger.warning(f"Health ping error: {e}")

# --- Основной цикл обновлений ---
@tasks.loop(minutes=5)
async def price_loop():
    async with aiohttp.ClientSession() as session:
        await update_price(session, "bitcoin", BTC_CHANNEL_ID)
        await update_price(session, "ethereum", ETH_CHANNEL_ID)

@tasks.loop(minutes=30)
async def fng_loop():
    async with aiohttp.ClientSession() as session:
        await update_fng(session)

@tasks.loop(minutes=15)
async def volume_loop():
    async with aiohttp.ClientSession() as session:
        await update_volumes(session)

@tasks.loop(minutes=4)
async def health_loop():
    await health_ping()

@bot.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {bot.user}")
    price_loop.start()
    fng_loop.start()
    volume_loop.start()
    health_loop.start()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

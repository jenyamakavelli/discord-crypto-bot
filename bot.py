import os
import asyncio
import logging
import aiohttp
import discord
from discord.ext import commands, tasks

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфиги из переменных окружения
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
CUSTOM_FNG_CHANNEL_ID = int(os.getenv("CUSTOM_FNG_CHANNEL_ID"))
BTC_VOLUME_CHANNEL_ID = int(os.getenv("BTC_VOLUME_CHANNEL_ID"))
ETH_VOLUME_CHANNEL_ID = int(os.getenv("ETH_VOLUME_CHANNEL_ID"))

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

HEALTH_URL = os.getenv("HEALTH_URL")

@tasks.loop(minutes=4)
async def self_ping():
    if not HEALTH_URL:
        logger.warning("HEALTH_URL не установлен, пинг не отправляется")
        return
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(HEALTH_URL) as resp:
                if resp.status == 200:
                    logger.info("Self-ping: 200 OK")
                else:
                    logger.warning(f"Self-ping: статус {resp.status}")
        except Exception as e:
            logger.warning(f"Ошибка self-ping: {e}")

@bot.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {bot.user}")
    update_prices_and_volumes.start()
    update_fng_indexes.start()
    self_ping.start()

# --- Функции получения данных ---

async def fetch_json(session, url):
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()
    except Exception as e:
        logger.warning(f"Ошибка запроса {url}: {e}")
        return None

async def get_prices(session):
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_vol=true"
    data = await fetch_json(session, url)
    if data:
        btc_price = data.get("bitcoin", {}).get("usd")
        eth_price = data.get("ethereum", {}).get("usd")
        btc_vol = data.get("bitcoin", {}).get("usd_24h_vol")
        eth_vol = data.get("ethereum", {}).get("usd_24h_vol")
        return btc_price, eth_price, btc_vol, eth_vol
    return None, None, None, None

async def get_fng(session):
    url = "https://api.alternative.me/fng/?limit=1"
    data = await fetch_json(session, url)
    if data and "data" in data and len(data["data"]) > 0:
        value = data["data"][0].get("value")
        return int(value)
    return None

# --- Кастомный модуль FNG (custom_fng) ---

async def get_custom_fng(session):
    # Тут вставить логику из твоего custom_fng.py, но адаптированную под async и с вызовом API
    # Пример (можно заменить на реальную логику твоего кастома):
    url = "https://api.somecustomfngsource.com/latest"  # пример, замени на реальный URL
    data = await fetch_json(session, url)
    if data and "custom_fng_value" in data:
        return int(data["custom_fng_value"])
    return None

# --- Вспомогательная функция для сокращения объёмов ---

def format_volume(vol):
    if vol is None:
        return "N/A"
    if vol >= 1e9:
        return f"${vol/1e9:.1f}B"
    if vol >= 1e6:
        return f"${vol/1e6:.1f}M"
    if vol >= 1e3:
        return f"${vol/1e3:.1f}K"
    return f"${vol:.0f}"

# --- Обновление каналов Discord ---

async def update_channel_name(channel_id, new_name):
    try:
        channel = bot.get_channel(channel_id)
        if channel and channel.name != new_name:
            await channel.edit(name=new_name)
            logger.info(f"Обновлено имя канала {channel_id}: {new_name}")
    except Exception as e:
        logger.warning(f"Ошибка обновления канала {channel_id}: {e}")

# --- Задачи с разными интервалами ---

@tasks.loop(minutes=5)
async def update_prices_and_volumes():
    async with aiohttp.ClientSession() as session:
        btc_price, eth_price, btc_vol, eth_vol = await get_prices(session)
        if btc_price:
            await update_channel_name(BTC_CHANNEL_ID, f"BTC: ${btc_price:,.0f}")
        if eth_price:
            await update_channel_name(ETH_CHANNEL_ID, f"ETH: ${eth_price:,.2f}")
        if btc_vol:
            await update_channel_name(BTC_VOLUME_CHANNEL_ID, f"BTC Vol: {format_volume(btc_vol)}")
        if eth_vol:
            await update_channel_name(ETH_VOLUME_CHANNEL_ID, f"ETH Vol: {format_volume(eth_vol)}")

@tasks.loop(minutes=30)
async def update_fng_indexes():
    async with aiohttp.ClientSession() as session:
        fng_value = await get_fng(session)
        custom_fng_value = await get_custom_fng(session)
        if fng_value is not None:
            await update_channel_name(FNG_CHANNEL_ID, f"Fear & Greed: {fng_value}")
        if custom_fng_value is not None:
            await update_channel_name(CUSTOM_FNG_CHANNEL_ID, f"Custom FNG: {custom_fng_value}")

# --- События бота ---

@bot.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {bot.user}")
    update_prices_and_volumes.start()
    update_fng_indexes.start()

# --- Запуск ---

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN не установлен в переменных окружения!")
        exit(1)
    bot.run(DISCORD_TOKEN)

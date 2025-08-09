import os
import asyncio
import logging
from discord.ext import commands, tasks
import discord
import aiohttp
from aiohttp import ClientSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ======= Конфиги из переменных окружения (Koyeb) =======
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))

BTC_VOLUME_CHANNEL_ID = int(os.getenv("BTC_VOLUME_CHANNEL_ID"))
ETH_VOLUME_CHANNEL_ID = int(os.getenv("ETH_VOLUME_CHANNEL_ID"))

HEALTH_URL = os.getenv("HEALTH_URL")  # Для Koyeb ping (чтобы не засыпал)

# ======= Интервалы обновлений (в секундах) =======
INTERVAL_PRICE = 5 * 60          # 5 минут
INTERVAL_FNG = 30 * 60           # 30 минут
INTERVAL_VOLUME = 15 * 60        # 15 минут
INTERVAL_HEALTH_PING = 4 * 60    # 4 минуты — чтобы Koyeb не засыпал

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ======= API URLs =======
COINGECKO_API = "https://api.coingecko.com/api/v3"
FNG_API = "https://api.alternative.me/fng/"

# ======= Кэш для отслеживания изменений =======
last_prices = {"btc": None, "eth": None}
last_fng = None
last_volumes = {"btc": None, "eth": None}

# ======= Форматирование объёмов =======
def format_volume(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    elif value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}K"
    else:
        return f"{value:.0f}"

# ======= Функции обновления =======
async def fetch_json(session: ClientSession, url: str):
    try:
        async with session.get(url, timeout=10) as resp:
            resp.raise_for_status()
            return await resp.json()
    except Exception as e:
        logger.warning(f"Ошибка запроса {url}: {e}")
        return None

async def update_price(session: ClientSession, coin_id: str, channel_id: int):
    global last_prices
    url = f"{COINGECKO_API}/simple/price?ids={coin_id}&vs_currencies=usd"
    data = await fetch_json(session, url)
    if not data or coin_id not in data:
        return
    price = data[coin_id]["usd"]
    if last_prices[coin_id] != price:
        last_prices[coin_id] = price
        channel = bot.get_channel(channel_id)
        if channel:
            text = f"{coin_id.upper()}: ${price:,.2f}"
            try:
                await channel.edit(name=text)
                logger.info(f"Обновлено имя канала {channel_id}: {text}")
            except discord.HTTPException as e:
                logger.warning(f"Discord rate limit или ошибка при обновлении канала {channel_id}: {e}")

async def update_fng(session: ClientSession):
    global last_fng
    data = await fetch_json(session, FNG_API)
    if not data or "data" not in data or len(data["data"]) == 0:
        return
    value = int(data["data"][0]["value"])
    if last_fng != value:
        last_fng = value
        channel = bot.get_channel(FNG_CHANNEL_ID)
        if channel:
            text = f"Fear & Greed: {value}"
            try:
                await channel.edit(name=text)
                logger.info(f"Обновлено имя канала {FNG_CHANNEL_ID}: {text}")
            except discord.HTTPException as e:
                logger.warning(f"Discord rate limit или ошибка при обновлении канала FNG: {e}")

async def update_volumes(session: ClientSession):
    global last_volumes
    url = f"{COINGECKO_API}/coins/markets?vs_currency=usd&ids=bitcoin,ethereum"
    data = await fetch_json(session, url)
    if not data or len(data) < 2:
        return

    btc_volume = data[0].get("total_volume", 0)
    eth_volume = data[1].get("total_volume", 0)

    formatted_btc_vol = f"${format_volume(btc_volume)}"
    formatted_eth_vol = f"${format_volume(eth_volume)}"

    if last_volumes["btc"] != btc_volume:
        last_volumes["btc"] = btc_volume
        channel = bot.get_channel(BTC_VOLUME_CHANNEL_ID)
        if channel:
            text = f"BTC Vol: {formatted_btc_vol}"
            try:
                await channel.edit(name=text)
                logger.info(f"Обновлено имя канала {BTC_VOLUME_CHANNEL_ID}: {text}")
            except discord.HTTPException as e:
                logger.warning(f"Discord rate limit или ошибка при обновлении канала BTC Vol: {e}")

    if last_volumes["eth"] != eth_volume:
        last_volumes["eth"] = eth_volume
        channel = bot.get_channel(ETH_VOLUME_CHANNEL_ID)
        if channel:
            text = f"ETH Vol: {formatted_eth_vol}"
            try:
                await channel.edit(name=text)
                logger.info(f"Обновлено имя канала {ETH_VOLUME_CHANNEL_ID}: {text}")
            except discord.HTTPException as e:
                logger.warning(f"Discord rate limit или ошибка при обновлении канала ETH Vol: {e}")

# ======= Задачи с нужными интервалами =======
@tasks.loop(seconds=INTERVAL_PRICE)
async def price_loop():
    async with aiohttp.ClientSession() as session:
        await update_price(session, "bitcoin", BTC_CHANNEL_ID)
        await update_price(session, "ethereum", ETH_CHANNEL_ID)

@tasks.loop(seconds=INTERVAL_FNG)
async def fng_loop():
    async with aiohttp.ClientSession() as session:
        await update_fng(session)

@tasks.loop(seconds=INTERVAL_VOLUME)
async def volume_loop():
    async with aiohttp.ClientSession() as session:
        await update_volumes(session)

# ======= Здоровый пинг для Koyeb =======
@tasks.loop(seconds=INTERVAL_HEALTH_PING)
async def health_ping_loop():
    if not HEALTH_URL:
        return
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(HEALTH_URL) as resp:
                if resp.status == 200:
                    logger.info("Health ping success")
                else:
                    logger.warning(f"Health ping status: {resp.status}")
        except Exception as e:
            logger.warning(f"Health ping error: {e}")

@bot.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {bot.user}")
    price_loop.start()
    fng_loop.start()
    volume_loop.start()
    health_ping_loop.start()

# ======= Запуск бота =======
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

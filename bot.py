import os
import asyncio
import logging
import aiohttp
import discord
from discord.ext import commands, tasks
from aiohttp import web

# ------------------ ЛОГИ ------------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)-8s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

# ------------------ ENV ------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))
PORT = int(os.getenv("PORT", 8000))  # Для health-check

# ------------------ DISCORD ------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ ПОЛУЧЕНИЕ ЦЕН ------------------
async def fetch_price(session, coin_id):
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": coin_id, "vs_currencies": "usd"}
    async with session.get(url, params=params) as resp:
        data = await resp.json()
        return data.get(coin_id, {}).get("usd")

async def update_channel_name(channel_id, name):
    channel = bot.get_channel(channel_id)
    if channel:
        await channel.edit(name=name)

# ------------------ ОБНОВЛЕНИЕ ЦЕН ------------------
@tasks.loop(minutes=5)
async def update_prices():
    log.info("🔄 Начинаю обновление цен")
    async with aiohttp.ClientSession() as session:
        btc_price = await fetch_price(session, "bitcoin")
        eth_price = await fetch_price(session, "ethereum")

    if btc_price is not None:
        await update_channel_name(BTC_CHANNEL_ID, f"BTC: ${btc_price:,.2f}")
        log.info(f"💰 Обновлено BTC: ${btc_price:,.2f}")
    else:
        log.warning("⚠️ Не удалось получить цену BTC")

    if eth_price is not None:
        await update_channel_name(ETH_CHANNEL_ID, f"ETH: ${eth_price:,.2f}")
        log.info(f"💰 Обновлено ETH: ${eth_price:,.2f}")
    else:
        log.warning("⚠️ Не удалось получить цену ETH")

@bot.event
async def on_ready():
    log.info(f"✅ Бот запущен как {bot.user}")
    update_prices.start()
    log.info("🟢 Задача обновления цен запущена")

# ------------------ HEALTH-CHECK ------------------
async def handle_health(request):
    return web.Response(text="OK", status=200)

async def start_webserver():
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"🌐 HTTP health-check сервер запущен на порту {PORT}")

# ------------------ ЗАПУСК ------------------
async def main():
    await start_webserver()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

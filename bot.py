import os
import asyncio
import aiohttp
import discord
from discord.ext import commands
from aiohttp import web

TOKEN = os.getenv("DISCORD_TOKEN")  # Токен бота
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

UPDATE_INTERVAL = 300  # секунды (5 минут)

# ───────── HTTP сервер для health-check Koyeb ─────────
async def handle(request):
    return web.Response(text="Bot is running")

def start_health_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)

    async def start():
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 8000)
        await site.start()
        print("🌐 HTTP health-check сервер запущен на порту 8000")

    asyncio.create_task(start())

# ───────── Получение цен ─────────
async def get_price(symbol):
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return float(data["price"])

# ───────── Bot ─────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Бот запущен как {bot.user}")

async def update_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            btc_price = await get_price("BTCUSDT")
            eth_price = await get_price("ETHUSDT")

            btc_channel = bot.get_channel(BTC_CHANNEL_ID)
            eth_channel = bot.get_channel(ETH_CHANNEL_ID)

            if btc_channel:
                await btc_channel.edit(name=f"BTC: ${btc_price:,.2f}")
                print(f"✅ Обновлено имя BTC канала: BTC: ${btc_price:,.2f}")

            if eth_channel:
                await eth_channel.edit(name=f"ETH: ${eth_price:,.2f}")
                print(f"✅ Обновлено имя ETH канала: ETH: ${eth_price:,.2f}")

        except Exception as e:
            print(f"⚠️ Ошибка обновления: {e}")

        await asyncio.sleep(UPDATE_INTERVAL)

@bot.event
async def setup_hook():
    start_health_server()
    asyncio.create_task(update_loop())

if __name__ == "__main__":
    bot.run(TOKEN)

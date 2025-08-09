from aiohttp import web
import asyncio
import discord
from discord.ext import tasks
import aiohttp as ahttp
import os

TOKEN = os.getenv("TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

intents = discord.Intents.default()
client = discord.Client(intents=intents)

async def get_prices():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd"
    async with ahttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["bitcoin"]["usd"], data["ethereum"]["usd"]

@tasks.loop(minutes=1)
async def update_prices():
    try:
        btc_price, eth_price = await get_prices()
        btc_channel = client.get_channel(BTC_CHANNEL_ID)
        eth_channel = client.get_channel(ETH_CHANNEL_ID)

        if btc_channel:
            await btc_channel.edit(name=f"BTC: ${btc_price:,.2f}")
            print(f"✅ Обновлено имя BTC канала: BTC: ${btc_price:,.2f}")
        else:
            print("⚠️ BTC канал не найден")

        if eth_channel:
            await eth_channel.edit(name=f"ETH: ${eth_price:,.2f}")
            print(f"✅ Обновлено имя ETH канала: ETH: ${eth_price:,.2f}")
        else:
            print("⚠️ ETH канал не найден")

    except Exception as e:
        print(f"⚠️ Ошибка обновления: {e}")

@client.event
async def on_ready():
    print(f"✅ Бот запущен как {client.user}")
    if not update_prices.is_running():
        update_prices.start()

# --- Health-check сервер ---
async def handle_healthcheck(request):
    return web.Response(text="OK")

async def start_healthcheck_server():
    app = web.Application()
    app.router.add_get("/", handle_healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    await site.start()
    print("🌐 HTTP health-check сервер запущен на порту 8000")

async def main():
    await start_healthcheck_server()
    await client.start(TOKEN)

import asyncio
asyncio.run(main())

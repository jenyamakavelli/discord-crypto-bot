import discord
import asyncio
import os
import aiohttp
from aiohttp import web

TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

client = discord.Client(intents=discord.Intents.default())

async def update_prices():
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.coindesk.com/v1/bpi/currentprice.json") as resp:
                    data = await resp.json()
                    btc_price = f"${float(data['bpi']['USD']['rate'].replace(',', '')):,.2f}"

                # ETH API
                async with session.get("https://api.coinbase.com/v2/prices/ETH-USD/spot") as resp:
                    data = await resp.json()
                    eth_price = f"${float(data['data']['amount']):,.2f}"

            btc_channel = client.get_channel(BTC_CHANNEL_ID)
            eth_channel = client.get_channel(ETH_CHANNEL_ID)

            if btc_channel:
                await btc_channel.edit(name=f"BTC: {btc_price}")
                print(f"✅ Обновлено имя BTC канала: BTC: {btc_price}")
            if eth_channel:
                await eth_channel.edit(name=f"ETH: {eth_price}")
                print(f"✅ Обновлено имя ETH канала: ETH: {eth_price}")

        except Exception as e:
            print(f"⚠️ Ошибка обновления: {e}")

        await asyncio.sleep(120)  # каждые 2 минуты

@client.event
async def on_ready():
    print(f"✅ Бот запущен как {client.user}")
    asyncio.create_task(update_prices())

# ---- HTTP Health Check ----
async def health(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 HTTP health-check сервер запущен на порту {port}")

async def main():
    await start_web_server()
    await client.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

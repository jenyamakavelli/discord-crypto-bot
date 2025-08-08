import discord
import aiohttp
import asyncio
import os
from aiohttp import web

TOKEN = os.getenv("TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

intents = discord.Intents.default()
client = discord.Client(intents=intents)

async def get_prices():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"CoinGecko API error {resp.status}: {text}")
            data = await resp.json()
            if "bitcoin" not in data or "ethereum" not in data:
                raise Exception(f"Unexpected response structure: {data}")
            btc_price = data["bitcoin"]["usd"]
            eth_price = data["ethereum"]["usd"]
            return btc_price, eth_price

async def update_prices():
    await client.wait_until_ready()
    while not client.is_closed():
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

        await asyncio.sleep(60)  # обновлять каждую минуту

@client.event
async def on_ready():
    print(f"✅ Бот запущен как {client.user}")

# Запускаем обновление цен через setup_hook (современный подход)
class MyClient(discord.Client):
    async def setup_hook(self):
        self.bg_task = self.loop.create_task(update_prices())

client = MyClient(intents=intents)

# HTTP сервер для health-check (Koyeb)
async def handle_healthcheck(request):
    return web.Response(text="OK")

app = web.Application()
app.router.add_get('/', handle_healthcheck)

def run():
    loop = asyncio.get_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, '0.0.0.0', 8000)
    loop.run_until_complete(site.start())
    print("🌐 HTTP health-check сервер запущен на порту 8000")
    loop.run_until_complete(client.start(TOKEN))

if __name__ == "__main__":
    run()

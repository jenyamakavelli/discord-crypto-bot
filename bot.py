import discord
from discord.ext import tasks
import aiohttp
import os

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
                raise Exception(f"Ошибка API: {resp.status}")
            data = await resp.json()
            return data["bitcoin"]["usd"], data["ethereum"]["usd"]

@tasks.loop(minutes=5)
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

client.run(TOKEN)

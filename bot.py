import discord
import requests
import asyncio
import os

TOKEN = os.getenv("TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

intents = discord.Intents.default()
client = discord.Client(intents=intents)

async def update_prices():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            btc = float(requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT").json()["price"])
            eth = float(requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT").json()["price"])

            btc_channel = client.get_channel(BTC_CHANNEL_ID)
            eth_channel = client.get_channel(ETH_CHANNEL_ID)

            if btc_channel:
                await btc_channel.edit(name=f"BTC: ${btc:,.2f}")
            if eth_channel:
                await eth_channel.edit(name=f"ETH: ${eth:,.2f}")

        except Exception as e:
            print(f"Ошибка: {e}")

        await asyncio.sleep(60)

@client.event
async def on_ready():
    print(f"✅ Бот запущен как {client.user}")

client.loop.create_task(update_prices())
client.run(TOKEN)

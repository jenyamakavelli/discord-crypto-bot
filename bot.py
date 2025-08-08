import discord
import requests
import os
from discord.ext import tasks

TOKEN = os.getenv("TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@tasks.loop(minutes=1)
async def update_prices():
    try:
        btc_price = float(requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT").json()["price"])
        eth_price = float(requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT").json()["price"])

        btc_channel = client.get_channel(BTC_CHANNEL_ID)
        eth_channel = client.get_channel(ETH_CHANNEL_ID)

        if btc_channel:
            await btc_channel.edit(name=f"BTC: ${btc_price:,.2f}")
        else:
            print("⚠️ BTC channel not found")

        if eth_channel:
            await eth_channel.edit(name=f"ETH: ${eth_price:,.2f}")
        else:
            print("⚠️ ETH channel not found")

    except Exception as e:
        print(f"Ошибка обновления цен: {e}")

@client.event
async def on_ready():
    print(f"✅ Бот запущен как {client.user}")
    update_prices.start()

client.run(TOKEN)

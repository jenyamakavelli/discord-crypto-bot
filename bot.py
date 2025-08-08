import discord
import requests
import os
from discord.ext import tasks

TOKEN = os.getenv("TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

intents = discord.Intents.default()
client = discord.Client(intents=intents)

def get_price(symbol: str) -> float | None:
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    headers = {"User-Agent": "CryptoPriceBot/1.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "price" in data:
            return float(data["price"])
        else:
            print(f"⚠️ Ошибка API Binance для {symbol}: {data}")
            return None
    except requests.RequestException as e:
        print(f"⚠️ Сетевая ошибка при запросе цены {symbol}: {e}")
        return None
    except ValueError as e:
        print(f"⚠️ Ошибка преобразования цены {symbol}: {e}")
        return None

@tasks.loop(minutes=1)
async def update_prices():
    btc_price = get_price("BTCUSDT")
    eth_price = get_price("ETHUSDT")

    if btc_price is None or eth_price is None:
        print("⚠️ Не удалось получить все цены, пропускаю обновление")
        return

    btc_channel = client.get_channel(BTC_CHANNEL_ID)
    eth_channel = client.get_channel(ETH_CHANNEL_ID)

    print(f"BTC channel: {btc_channel}, ETH channel: {eth_channel}")

    try:
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

    except discord.Forbidden:
        print("⚠️ Нет прав на редактирование канала")
    except discord.HTTPException as e:
        print(f"⚠️ Ошибка Discord API при обновлении каналов: {e}")

@client.event
async def on_ready():
    print(f"✅ Бот запущен как {client.user}")
    update_prices.start()

client.run(TOKEN)

import discord
import aiohttp
import asyncio
import os

TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

intents = discord.Intents.default()
client = discord.Client(intents=intents)

async def fetch_price(session, coin):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin}&vs_currencies=usd"
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"⚠️ Ошибка API CoinGecko: {resp.status}")
                return None
            data = await resp.json()
            return data[coin]["usd"]
    except Exception as e:
        print(f"⚠️ Ошибка получения цены {coin}: {e}")
        return None

async def update_prices():
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while not client.is_closed():
            try:
                btc_price = await fetch_price(session, "bitcoin")
                eth_price = await fetch_price(session, "ethereum")

                if btc_price is not None:
                    try:
                        btc_channel = client.get_channel(BTC_CHANNEL_ID)
                        await btc_channel.edit(name=f"BTC: ${btc_price:,.2f}")
                        print(f"✅ Обновлено имя BTC канала: BTC: ${btc_price:,.2f}")
                    except discord.errors.Forbidden:
                        print("⚠️ Нет прав на редактирование BTC канала")

                if eth_price is not None:
                    try:
                        eth_channel = client.get_channel(ETH_CHANNEL_ID)
                        await eth_channel.edit(name=f"ETH: ${eth_price:,.2f}")
                        print(f"✅ Обновлено имя ETH канала: ETH: ${eth_price:,.2f}")
                    except discord.errors.Forbidden:
                        print("⚠️ Нет прав на редактирование ETH канала")

            except discord.errors.HTTPException as e:
                if e.status == 429:
                    retry_after = int(e.response.headers.get("Retry-After", 10))
                    print(f"⚠️ Rate limit! Ждём {retry_after} сек.")
                    await asyncio.sleep(retry_after)
                    continue
                else:
                    print(f"⚠️ HTTP ошибка: {e}")

            await asyncio.sleep(120)  # обновляем каждые 2 минуты

@client.event
async def on_ready():
    print(f"✅ Бот запущен как {client.user}")

@client.event
async def setup_hook():
    client.loop.create_task(update_prices())

client.run(TOKEN)

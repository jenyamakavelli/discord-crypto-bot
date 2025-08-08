import os
import asyncio
import discord
import aiohttp
from aiohttp import web

TOKEN = os.getenv("DISCORD_TOKEN")  # Токен бота
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))  # ID канала BTC
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))  # ID канала ETH
UPDATE_INTERVAL = 300  # раз в 5 минут (чтобы не ловить rate-limit)

intents = discord.Intents.default()
client = discord.Client(intents=intents)

# Храним прошлые цены, чтобы не спамить Discord
last_btc_price = None
last_eth_price = None

# Health-check для Koyeb
async def healthcheck(request):
    return web.Response(text="OK")

async def run_healthcheck_server():
    app = web.Application()
    app.router.add_get("/", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    await site.start()
    print("🌐 HTTP health-check сервер запущен на порту 8000")

async def fetch_price(session, coin_id):
    """Получить цену криптовалюты с CoinGecko"""
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    try:
        async with session.get(url, ssl=False, timeout=10) as resp:
            if resp.status != 200:
                print(f"⚠️ Ошибка API CoinGecko: {resp.status}")
                return None
            data = await resp.json()
            return f"${data[coin_id]['usd']:,.2f}"
    except Exception as e:
        print(f"⚠️ Ошибка получения {coin_id.upper()}: {e}")
        return None

async def update_prices():
    global last_btc_price, last_eth_price
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while not client.is_closed():
            btc_price = await fetch_price(session, "bitcoin")
            eth_price = await fetch_price(session, "ethereum")

            if btc_price and btc_price != last_btc_price:
                try:
                    channel = client.get_channel(BTC_CHANNEL_ID)
                    if channel:
                        await channel.edit(name=f"BTC: {btc_price}")
                        print(f"✅ Обновлено имя BTC канала: BTC: {btc_price}")
                        last_btc_price = btc_price
                except discord.Forbidden:
                    print("⚠️ Нет прав на редактирование BTC канала")
                except Exception as e:
                    print(f"⚠️ Ошибка обновления BTC канала: {e}")

            if eth_price and eth_price != last_eth_price:
                try:
                    channel = client.get_channel(ETH_CHANNEL_ID)
                    if channel:
                        await channel.edit(name=f"ETH: {eth_price}")
                        print(f"✅ Обновлено имя ETH канала: ETH: {eth_price}")
                        last_eth_price = eth_price
                except discord.Forbidden:
                    print("⚠️ Нет прав на редактирование ETH канала")
                except Exception as e:
                    print(f"⚠️ Ошибка обновления ETH канала: {e}")

            await asyncio.sleep(UPDATE_INTERVAL)

@client.event
async def on_ready():
    print(f"✅ Бот запущен как {client.user}")
    asyncio.create_task(update_prices())

async def main():
    await run_healthcheck_server()
    await client.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

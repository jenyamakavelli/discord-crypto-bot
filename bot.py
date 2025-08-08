import discord
import aiohttp
import asyncio
import os

TOKEN = os.getenv("TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

intents = discord.Intents.default()
client = discord.Client(intents=intents)

last_btc_price = None
last_eth_price = None

async def fetch_price(session, coin_id):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                print(f"⚠️ Ошибка сети при запросе {coin_id}: HTTP {resp.status}")
                return None
            data = await resp.json()
            return data.get(coin_id, {}).get("usd")
    except Exception as e:
        print(f"⚠️ Ошибка при запросе {coin_id}: {e}")
        return None

async def update_prices():
    global last_btc_price, last_eth_price
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while not client.is_closed():
            try:
                btc_price = await fetch_price(session, "bitcoin")
                eth_price = await fetch_price(session, "ethereum")

                if btc_price is None or eth_price is None:
                    print("⚠️ Не удалось получить цены, пропускаю обновление")
                else:
                    btc_channel = client.get_channel(BTC_CHANNEL_ID)
                    eth_channel = client.get_channel(ETH_CHANNEL_ID)

                    # Обновляем BTC только если цена изменилась более чем на 0.1%
                    if last_btc_price is None or abs(btc_price - last_btc_price) / last_btc_price > 0.001:
                        if btc_channel:
                            try:
                                await btc_channel.edit(name=f"BTC: ${btc_price:,.2f}")
                                print(f"✅ Обновлено имя BTC канала: BTC: ${btc_price:,.2f}")
                                last_btc_price = btc_price
                            except discord.errors.Forbidden:
                                print("⚠️ Нет прав на редактирование BTC канала")
                            except discord.errors.HTTPException as e:
                                if e.status == 429:
                                    retry_after = float(e.response.headers.get('Retry-After', '60'))
                                    print(f"⚠️ Rate limited при обновлении BTC. Жду {retry_after} секунд...")
                                    await asyncio.sleep(retry_after)
                                    continue
                                else:
                                    print(f"⚠️ Ошибка HTTP при обновлении BTC: {e}")

                    # Обновляем ETH только если цена изменилась более чем на 0.1%
                    if last_eth_price is None or abs(eth_price - last_eth_price) / last_eth_price > 0.001:
                        if eth_channel:
                            try:
                                await eth_channel.edit(name=f"ETH: ${eth_price:,.2f}")
                                print(f"✅ Обновлено имя ETH канала: ETH: ${eth_price:,.2f}")
                                last_eth_price = eth_price
                            except discord.errors.Forbidden:
                                print("⚠️ Нет прав на редактирование ETH канала")
                            except discord.errors.HTTPException as e:
                                if e.status == 429:
                                    retry_after = float(e.response.headers.get('Retry-After', '60'))
                                    print(f"⚠️ Rate limited при обновлении ETH. Жду {retry_after} секунд...")
                                    await asyncio.sleep(retry_after)
                                    continue
                                else:
                                    print(f"⚠️ Ошибка HTTP при обновлении ETH: {e}")

            except Exception as e:
                print(f"⚠️ Общая ошибка в update_prices: {e}")

            await asyncio.sleep(600)  # 10 минут между обновлениями

@client.event
async def on_ready():
    print(f"✅ Бот запущен как {client.user}")

client.loop.create_task(update_prices())
client.run(TOKEN)

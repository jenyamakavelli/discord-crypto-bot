import os
import discord
from discord.ext import tasks
import aiohttp
import logging
import asyncio

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

intents = discord.Intents.default()
client = discord.Client(intents=intents)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

async def get_prices():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP error {resp.status} fetching prices")
            data = await resp.json()
            btc_price = data['bitcoin']['usd']
            eth_price = data['ethereum']['usd']
            return btc_price, eth_price

@tasks.loop(minutes=3)
async def update_prices():
    logging.info("🔄 Начинаю обновление цен")
    try:
        btc_price, eth_price = await get_prices()
        logging.info(f"💰 Получены цены BTC: {btc_price}, ETH: {eth_price}")

        btc_channel = client.get_channel(BTC_CHANNEL_ID)
        eth_channel = client.get_channel(ETH_CHANNEL_ID)

        if btc_channel:
            try:
                await btc_channel.edit(name=f"BTC: ${btc_price:,.2f}")
                logging.info(f"✅ Обновлено имя BTC канала: BTC: ${btc_price:,.2f}")
            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = int(e.response.headers.get("Retry-After", "10"))
                    logging.warning(f"⚠️ Rate limited on BTC channel update, retrying after {retry_after} seconds")
                    await asyncio.sleep(retry_after)
                else:
                    logging.error(f"Ошибка обновления BTC канала: {e}")
        else:
            logging.warning("⚠️ BTC канал не найден")

        if eth_channel:
            try:
                await eth_channel.edit(name=f"ETH: ${eth_price:,.2f}")
                logging.info(f"✅ Обновлено имя ETH канала: ETH: ${eth_price:,.2f}")
            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = int(e.response.headers.get("Retry-After", "10"))
                    logging.warning(f"⚠️ Rate limited on ETH channel update, retrying after {retry_after} seconds")
                    await asyncio.sleep(retry_after)
                else:
                    logging.error(f"Ошибка обновления ETH канала: {e}")
        else:
            logging.warning("⚠️ ETH канал не найден")

    except Exception as e:
        logging.error(f"⚠️ Ошибка обновления: {e}")

    logging.info("✅ Обновление цен завершено")

@client.event
async def on_ready():
    logging.info(f"✅ Бот запущен как {client.user}")
    if not update_prices.is_running():
        update_prices.start()
        logging.info("🟢 Задача обновления цен запущена")

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)

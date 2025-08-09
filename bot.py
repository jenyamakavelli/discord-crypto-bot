import os
import discord
from discord.ext import tasks
import aiohttp
import logging

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))

intents = discord.Intents.default()
client = discord.Client(intents=intents)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# Для хранения последних цен
last_prices = {
    "btc": None,
    "eth": None
}

PRICE_UPDATE_THRESHOLD = 0.005  # 0.5% изменения цены

async def get_prices():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"Ошибка HTTP {resp.status} при запросе цен")
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

        # Проверка, изменились ли цены более чем на 0.5%
        def significant_change(old, new):
            if old is None:
                return True
            return abs(new - old) / old >= PRICE_UPDATE_THRESHOLD

        if btc_channel and significant_change(last_prices['btc'], btc_price):
            await btc_channel.edit(name=f"BTC: ${btc_price:,.2f}")
            logging.info(f"✅ Обновлено имя BTC канала: BTC: ${btc_price:,.2f}")
            last_prices['btc'] = btc_price
        else:
            logging.info("ℹ️ Изменение цены BTC незначительное или канал не найден")

        if eth_channel and significant_change(last_prices['eth'], eth_price):
            await eth_channel.edit(name=f"ETH: ${eth_price:,.2f}")
            logging.info(f"✅ Обновлено имя ETH канала: ETH: ${eth_price:,.2f}")
            last_prices['eth'] = eth_price
        else:
            logging.info("ℹ️ Изменение цены ETH незначительное или канал не найден")

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

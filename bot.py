import os
import asyncio
import logging
import threading
from discord.ext import commands, tasks
from flask import Flask, jsonify
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

# Конфиги из окружения — обязательно задавай в настройках Koyeb
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))  # канал для Fear&Greed индекса
COINGECKO_VOLUME_CHANNEL_ID = int(os.getenv("COINGECKO_VOLUME_CHANNEL_ID"))

# Flask Health-check сервер
app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

def run_flask():
    app.run(host="0.0.0.0", port=8000)

threading.Thread(target=run_flask, daemon=True).start()

intents = commands.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Последние сохранённые значения, чтобы не спамить обновления
last_prices = {"btc": None, "eth": None}
last_fng = None
last_volume = None

# Интервалы обновления в минутах
UPDATE_INTERVAL_PRICES = 3
UPDATE_INTERVAL_FNG = 15
UPDATE_INTERVAL_VOLUME = 10


def get_prices():
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": "bitcoin,ethereum",
        "vs_currencies": "usd",
        "include_24hr_vol": "true"
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    return {
        "btc": data["bitcoin"]["usd"],
        "eth": data["ethereum"]["usd"],
        "btc_vol": data["bitcoin"].get("usd_24h_vol"),
        "eth_vol": data["ethereum"].get("usd_24h_vol"),
    }


def get_fng_index():
    url = "https://api.alternative.me/fng/"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    return int(data["data"][0]["value"])


def get_coingecko_volume():
    # Собираем суммарный 24ч объём BTC+ETH с CoinGecko
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": "bitcoin,ethereum",
        "vs_currencies": "usd",
        "include_24hr_vol": "true"
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    btc_vol = data["bitcoin"].get("usd_24h_vol", 0)
    eth_vol = data["ethereum"].get("usd_24h_vol", 0)
    total_vol = btc_vol + eth_vol
    return total_vol


async def update_prices_task():
    global last_prices
    while True:
        try:
            logger.info("🔄 Обновляю цены BTC и ETH...")
            prices = get_prices()
            btc_price = prices["btc"]
            eth_price = prices["eth"]

            channel_btc = await bot.fetch_channel(BTC_CHANNEL_ID)
            channel_eth = await bot.fetch_channel(ETH_CHANNEL_ID)

            # Обновляем, только если цена изменилась (можно убрать проверку, если хочешь всегда обновлять)
            if last_prices["btc"] != btc_price:
                await channel_btc.edit(name=f"BTC: ${btc_price:,.0f}")
                logger.info(f"Обновлено имя канала {BTC_CHANNEL_ID}: BTC: ${btc_price:,.0f}")
                last_prices["btc"] = btc_price
            if last_prices["eth"] != eth_price:
                await channel_eth.edit(name=f"ETH: ${eth_price:,.2f}")
                logger.info(f"Обновлено имя канала {ETH_CHANNEL_ID}: ETH: ${eth_price:,.2f}")
                last_prices["eth"] = eth_price

        except Exception as e:
            logger.warning(f"Ошибка обновления цен: {e}")
        await asyncio.sleep(UPDATE_INTERVAL_PRICES * 60)


async def update_fng_task():
    global last_fng
    while True:
        try:
            logger.info("🔄 Обновляю индекс страха и жадности...")
            fng = get_fng_index()
            channel = await bot.fetch_channel(FNG_CHANNEL_ID)
            if last_fng != fng:
                await channel.edit(name=f"Fear & Greed: {fng}")
                logger.info(f"Обновлено имя канала {FNG_CHANNEL_ID}: Fear & Greed: {fng}")
                last_fng = fng
        except Exception as e:
            logger.warning(f"Ошибка обновления FNG: {e}")
        await asyncio.sleep(UPDATE_INTERVAL_FNG * 60)


async def update_volume_task():
    global last_volume
    while True:
        try:
            logger.info("🔄 Обновляю объёмы торгов CoinGecko...")
            volume = get_coingecko_volume()
            channel = await bot.fetch_channel(COINGECKO_VOLUME_CHANNEL_ID)
            if last_volume != volume:
                vol_str = f"{volume/1_000_000_000:.2f}B $24h vol"
                await channel.edit(name=f"Volume CG: {vol_str}")
                logger.info(f"Обновлено имя канала {COINGECKO_VOLUME_CHANNEL_ID}: Volume CG: {vol_str}")
                last_volume = volume
        except Exception as e:
            logger.warning(f"Ошибка обновления объёмов: {e}")
        await asyncio.sleep(UPDATE_INTERVAL_VOLUME * 60)


@bot.event
async def on_ready():
    logger.info(f"✅ Бот запущен как {bot.user}")
    # Запускаем задачи обновления параллельно
    bot.loop.create_task(update_prices_task())
    bot.loop.create_task(update_fng_task())
    bot.loop.create_task(update_volume_task())


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

import os
import asyncio
import logging
from discord.ext import commands, tasks
import discord
import aiohttp

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("DISCORD_TOKEN")

# ID голосовых каналов для изменения названий
BTC_CHANNEL_ID = int(os.getenv("BTC_CHANNEL_ID"))
ETH_CHANNEL_ID = int(os.getenv("ETH_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
BINANCE_VOL_CHANNEL_ID = int(os.getenv("BINANCE_VOL_CHANNEL_ID"))
COINBASE_VOL_CHANNEL_ID = int(os.getenv("COINBASE_VOL_CHANNEL_ID"))
ONCHAIN_BTC_CHANNEL_ID = int(os.getenv("ONCHAIN_BTC_CHANNEL_ID"))
ONCHAIN_ETH_CHANNEL_ID = int(os.getenv("ONCHAIN_ETH_CHANNEL_ID"))

intents = discord.Intents.default()
client = commands.Bot(command_prefix="!", intents=intents)

# Хранилище последних значений для проверки изменений (чтобы не спамить обновления)
last_prices = {"btc": None, "eth": None}
last_fng = None
last_binance_vol = None
last_coinbase_vol = None
last_onchain_btc = None
last_onchain_eth = None

@client.event
async def on_ready():
    logging.info(f"✅ Бот запущен как {client.user}")
    update_prices.start()
    update_fng.start()
    update_exchange_volumes.start()
    update_onchain_data.start()

async def fetch_json(url, session):
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                logging.warning(f"Ошибка HTTP {resp.status} для {url}")
                return None
            return await resp.json()
    except Exception as e:
        logging.warning(f"Ошибка запроса {url}: {e}")
        return None

# --- Цены BTC и ETH с CoinGecko ---
@tasks.loop(minutes=3)
async def update_prices():
    logging.info("🔄 Обновляю цены BTC и ETH...")
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd"
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(url, session)
        if not data:
            return
        btc_price = data.get("bitcoin", {}).get("usd")
        eth_price = data.get("ethereum", {}).get("usd")
        if btc_price and eth_price:
            # Проверяем изменение >0.01% для обновления (чтобы избежать частых обновлений)
            update_btc = should_update(last_prices["btc"], btc_price, 0.0001)
            update_eth = should_update(last_prices["eth"], eth_price, 0.0001)

            if update_btc:
                await update_channel_name(BTC_CHANNEL_ID, f"BTC: ${btc_price:,.2f}")
                last_prices["btc"] = btc_price
                logging.info(f"Обновлено имя BTC канала: BTC: ${btc_price:,.2f}")
            if update_eth:
                await update_channel_name(ETH_CHANNEL_ID, f"ETH: ${eth_price:,.2f}")
                last_prices["eth"] = eth_price
                logging.info(f"Обновлено имя ETH канала: ETH: ${eth_price:,.2f}")

def should_update(last_value, new_value, threshold):
    if last_value is None:
        return True
    change = abs(new_value - last_value) / last_value
    return change >= threshold

async def update_channel_name(channel_id, new_name):
    channel = client.get_channel(channel_id)
    if channel is None:
        logging.warning(f"Канал с ID {channel_id} не найден")
        return
    try:
        await channel.edit(name=new_name)
    except discord.HTTPException as e:
        logging.warning(f"Ошибка обновления канала {channel_id}: {e}")

# --- Индекс страха и жадности (Fear & Greed Index) ---
@tasks.loop(hours=1)
async def update_fng():
    logging.info("🔄 Обновляю индекс страха и жадности...")
    url = "https://api.alternative.me/fng/?limit=1"
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(url, session)
        if not data or "data" not in data:
            return
        fng_value = data["data"][0].get("value")
        fng_class = data["data"][0].get("value_classification")
        if fng_value and fng_class:
            text = f"F&G: {fng_value} ({fng_class})"
            if last_fng != text:
                await update_channel_name(FNG_CHANNEL_ID, text)
                global last_fng
                last_fng = text
                logging.info(f"Обновлено имя FNG канала: {text}")

# --- Объёмы торгов Binance и Coinbase ---
@tasks.loop(minutes=5)
async def update_exchange_volumes():
    logging.info("🔄 Обновляю объёмы торгов Binance и Coinbase...")
    binance_vol = await fetch_binance_volume()
    coinbase_vol = await fetch_coinbase_volume()
    if binance_vol is not None:
        if should_update(last_binance_vol, binance_vol, 0.01):
            await update_channel_name(BINANCE_VOL_CHANNEL_ID, f"Binance Vol: ${binance_vol:,.0f}")
            global last_binance_vol
            last_binance_vol = binance_vol
            logging.info(f"Обновлено имя Binance Vol канала: ${binance_vol:,.0f}")
    if coinbase_vol is not None:
        if should_update(last_coinbase_vol, coinbase_vol, 0.01):
            await update_channel_name(COINBASE_VOL_CHANNEL_ID, f"Coinbase Vol: ${coinbase_vol:,.0f}")
            global last_coinbase_vol
            last_coinbase_vol = coinbase_vol
            logging.info(f"Обновлено имя Coinbase Vol канала: ${coinbase_vol:,.0f}")

async def fetch_binance_volume():
    url = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(url, session)
        if data:
            try:
                vol = float(data.get("quoteVolume", 0))
                return vol
            except:
                return None
    return None

async def fetch_coinbase_volume():
    url = "https://api.pro.coinbase.com/products/BTC-USD/stats"
    async with aiohttp.ClientSession() as session:
        data = await fetch_json(url, session)
        if data:
            try:
                vol = float(data.get("volume", 0)) * float(data.get("last", 0))
                return vol
            except:
                return None
    return None

# --- On-chain активность — кол-во активных адресов BTC и ETH ---
@tasks.loop(minutes=30)
async def update_onchain_data():
    logging.info("🔄 Обновляю on-chain данные (активные адреса BTC/ETH)...")
    btc_active = await fetch_onchain_active_addresses("btc")
    eth_active = await fetch_onchain_active_addresses("eth")
    if btc_active is not None:
        if should_update(last_onchain_btc, btc_active, 0.01):
            await update_channel_name(ONCHAIN_BTC_CHANNEL_ID, f"BTC Active Addr: {btc_active:,}")
            global last_onchain_btc
            last_onchain_btc = btc_active
            logging.info(f"Обновлено имя ONCHAIN BTC канала: {btc_active:,}")
    if eth_active is not None:
        if should_update(last_onchain_eth, eth_active, 0.01):
            await update_channel_name(ONCHAIN_ETH_CHANNEL_ID, f"ETH Active Addr: {eth_active:,}")
            global last_onchain_eth
            last_onchain_eth = eth_active
            logging.info(f"Обновлено имя ONCHAIN ETH канала: {eth_active:,}")

async def fetch_onchain_active_addresses(chain):
    if chain == "btc":
        url = "https://api.blockchain.info/q/getreceivedbyaddresscount"  # фиктивный пример, заменить реальным API
        # Лучше использовать Glassnode или CryptoQuant API (нужен ключ) — ниже пример для Glassnode
        # url = f"https://api.glassnode.com/v1/metrics/addresses/active_count?a=BTC&api_key=YOUR_API_KEY"
    elif chain == "eth":
        url = "https://api.etherscan.io/api?module=stats&action=activeaddress&apikey=YOUR_ETHERSCAN_API_KEY"
    else:
        return None

    async with aiohttp.ClientSession() as session:
        data = await fetch_json(url, session)
        if data:
            # Разбор конкретного API нужно настраивать под источник
            # Вернем примерное число
            if chain == "btc":
                return int(data)  # в случае blockchain.info
            elif chain == "eth":
                try:
                    return int(data.get("result"))
                except:
                    return None
        return None

if __name__ == "__main__":
    client.run(TOKEN)

import os
import logging
import asyncio
import aiohttp
import discord
from discord.ext import tasks, commands
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# =============== CONFIG ===============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_PRICE_CHANNEL_ID = int(os.getenv("BTC_PRICE_CHANNEL_ID"))
ETH_PRICE_CHANNEL_ID = int(os.getenv("ETH_PRICE_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
BTC_VOL_CHANNEL_ID = int(os.getenv("BTC_VOL_CHANNEL_ID"))
ETH_VOL_CHANNEL_ID = int(os.getenv("ETH_VOL_CHANNEL_ID"))
SESSIONS_CHANNEL_ID = int(os.getenv("SESSIONS_CHANNEL_ID"))  # канал для pinned message с сессиями
HEALTH_URL = os.getenv("HEALTH_URL")  # Для Koyeb Ping
# =====================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== Flask health server =====
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=8000)

Thread(target=run_flask, daemon=True).start()

# ===== Shared state =====
last_values = {
    "btc_price": None,
    "eth_price": None,
    "btc_vol": None,
    "eth_vol": None,
    "fng": None,
}

# ===== Async HTTP fetch with retry & backoff =====
async def fetch_json(session, url, max_retries=5):
    backoff = 1
    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", backoff))
                    logger.warning(f"429 rate limited by API, sleeping {retry_after} sec")
                    await asyncio.sleep(retry_after)
                    backoff = min(backoff * 2, 60)
                    continue
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"HTTP error {e} on attempt {attempt+1} for URL {url}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    logger.error(f"Failed to fetch {url} after {max_retries} attempts")
    return None

# ===== Data fetchers =====
async def get_price_and_volume(session, coin_id):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
    data = await fetch_json(session, url)
    if not data:
        return None, None
    try:
        price = data["market_data"]["current_price"]["usd"]
        volume = data["market_data"]["total_volume"]["usd"]
        return price, volume
    except KeyError:
        logger.warning(f"Malformed data from CoinGecko for {coin_id}")
        return None, None

async def get_fear_and_greed(session):
    url = "https://api.alternative.me/fng/"
    data = await fetch_json(session, url)
    if not data:
        return None
    try:
        return int(data["data"][0]["value"])
    except (KeyError, IndexError, ValueError):
        logger.warning("Malformed Fear & Greed Index data")
        return None

# ===== Helper to format volume =====
def format_volume(vol):
    if vol >= 1_000_000_000:
        return f"${vol/1_000_000_000:.1f}B"
    elif vol >= 1_000_000:
        return f"${vol/1_000_000:.1f}M"
    else:
        return f"${vol:,.0f}"

# ===== Update channel only if value changed =====
async def update_channel_if_changed(channel_id, new_name, key):
    if last_values.get(key) != new_name:
        channel = bot.get_channel(channel_id)
        if channel:
            try:
                await channel.edit(name=new_name)
                last_values[key] = new_name
                logger.info(f"Updated channel {channel_id}: {new_name}")
            except discord.HTTPException as e:
                logger.error(f"Error updating channel {channel_id}: {e}")

# ===== Market sessions logic =====
def seconds_to_dhm(seconds: int):
    days = seconds // 86400
    seconds %= 86400
    hours = seconds // 3600
    seconds %= 3600
    minutes = seconds // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

def get_forex_sessions_utc(now=None):
    # Forex сессии UTC время (всегда в UTC):
    # Tokyo: 00:00 - 09:00 UTC (00:00 понедельник - 09:00 пятница)
    # London: 08:00 - 17:00 UTC (08:00 понедельник - 17:00 пятница)
    # New York: 13:00 - 22:00 UTC (13:00 понедельник - 22:00 пятница)
    # Выходные с пятницы 22:00 по воскресенье 22:00 UTC
    # Все даты и время - UTC

    if now is None:
        now = datetime.utcnow()

    # Определим базовые времена открытия и закрытия (для текущего или следующего рабочего дня)
    weekday = now.weekday()  # 0=понедельник .. 6=воскресенье
    time_seconds = now.hour * 3600 + now.minute * 60 + now.second

    # Проверка на выходные Forex (пятница 22:00 - воскресенье 22:00)
    friday_close_time = (4 * 86400) + (22 * 3600)  # пятница 22:00 в секундах с начала недели
    sunday_open_time = (6 * 86400) + (22 * 3600)   # воскресенье 22:00 в секундах с начала недели

    now_seconds = weekday * 86400 + time_seconds

    forex_closed = friday_close_time <= now_seconds < sunday_open_time

    sessions = {
        "Tokyo": {"open": (0 * 86400) + (0 * 3600), "close": (0 * 86400) + (9 * 3600)},    # 00:00-09:00 UTC каждый день (Пон-Пят)
        "London": {"open": (0 * 86400) + (8 * 3600), "close": (0 * 86400) + (17 * 3600)},  # 08:00-17:00 UTC
        "New York": {"open": (0 * 86400) + (13 * 3600), "close": (0 * 86400) + (22 * 3600)}, # 13:00-22:00 UTC
    }

    # Для дней с понедельника по пятницу
    # Расчёт открытия и закрытия с учётом дня недели и выходных
    result = {}

    def get_next_open_close(session_name):
        # Сессии работают только с Понедельника по Пятницу,
        # С пятницы 22:00 по воскресенье 22:00 рынок закрыт

        # Текущее время в секундах с начала недели:
        # Относительные секунды открытия и закрытия сессии для каждого дня

        # Найдем ближайший открывающийся слот и закрывающийся слот для сессии

        # Поскольку сессии всегда с 0 до 9, 8-17, 13-22 часов (UTC),
        # смещаем на конкретный день для вычисления времени

        # Сессии открыты Пн-Пт, то есть дни 0..4
        # Нужно определить, открыт ли сейчас рынок и сколько осталось до открытия/закрытия

        # Определим ближайший open и close в будущем относительно now

        # Функция возвращает (status:str, text:str) например "open", "closes in 5h 3m" и тд

        # Найдем текущее время в секундах от начала недели
        now_week_sec = weekday * 86400 + time_seconds

        # Сессия открыта, если сейчас в пределах open-close для сегодняшнего дня и сегодня пн-пт
        today_open_sec = weekday * 86400 + sessions[session_name]["open"]
        today_close_sec = weekday * 86400 + sessions[session_name]["close"]

        # Проверим открыт ли сейчас рынок для этой сессии
        if 0 <= weekday <= 4 and today_open_sec <= now_week_sec < today_close_sec:
            # открыт, считаем сколько осталось до закрытия
            secs_left = today_close_sec - now_week_sec
            return ("open", f"closes in {seconds_to_dhm(secs_left)}")

        # Если сейчас выходные
        if forex_closed:
            # Рынок закрыт до воскресенья 22:00 (6-й день недели 22:00)
            # Отсчёт до открытия сессии в понедельник
            # Понедельник — 0-й день
            # Для открытия сессии понедельника добавляем дни до понедельника + offset открытие сессии
            next_open_weekday = 0
            next_open_sec = next_open_weekday * 86400 + sessions[session_name]["open"]

            # Добавим неделю к now_week_sec, если next_open_sec <= now_week_sec (т.к. мы сейчас в выходные, но open раньше понедельника)
            if next_open_sec <= now_week_sec:
                next_open_sec += 7 * 86400

            diff = next_open_sec - now_week_sec
            return ("closed", f"opens in {seconds_to_dhm(diff)}")

        # Если сейчас рабочий день, но рынок ещё не открылся или уже закрылся сегодня

        # Если сейчас до открытия сегодня
        if 0 <= weekday <= 4 and now_week_sec < today_open_sec:
            diff = today_open_sec - now_week_sec
            return ("closed", f"opens in {seconds_to_dhm(diff)}")

        # Если сегодня уже после закрытия, нужно найти следующий рабочий день с открытием
        # Найдем следующий рабочий день (следующий день понедельник-пятница)
        next_day = (weekday + 1) % 7
        days_ahead = 1
        while next_day > 4:  # пропускаем выходные
            next_day = (next_day + 1) % 7
            days_ahead += 1

        next_open_sec = next_day * 86400 + sessions[session_name]["open"]
        diff = next_open_sec - now_week_sec
        return ("closed", f"opens in {seconds_to_dhm(diff)}")

    for session_name in sessions.keys():
        status, rel_time = get_next_open_close(session_name)
        result[session_name] = {"status": status, "relative": rel_time}

    return result

# ===== Update pinned message with sessions countdown =====
async def update_sessions_message():
    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not channel:
        logger.warning("Sessions channel not found")
        return

    pinned = await channel.pins()
    pinned_msg = pinned[0] if pinned else None

    now = datetime.utcnow().replace(second=0, microsecond=0)
    sessions = get_forex_sessions_utc(now)

    # Время последнего обновления для заголовка
    # last_update — храним либо как глобальную переменную, либо просто сейчас
    # Для демонстрации используем now (текущий момент)
    # Для динамичного "обновлено N мин назад" нужно сохранять время последнего обновления в переменную
    # Допустим last_update_dt = now (т.к. обновляем каждую минуту)
    last_update_dt = now

    updated_text = format_updated_since(last_update_dt, now)

    lines = [
        f"🕒 Market sessions (relative times, UTC) — {updated_text}",
        ""
    ]

    for session_name, info in sessions.items():
        emoji = get_session_status_emoji(info['status'], info['relative'])
        lines.append(f"{emoji} {session_name}: {info['status']} — {info['relative']}")

    lines.append("")
    lines.append("⚠️ Countdown is relative (D days Hh Mm). Gap alerts posted for session opens.")

    content = "\n".join(lines)

    try:
        if pinned_msg:
            await pinned_msg.edit(content=content)
            logger.info("Updated pinned sessions message")
        else:
            msg = await channel.send(content)
            await msg.pin()
            logger.info("Pinned new sessions message")
    except discord.HTTPException as e:
        logger.error(f"Failed to update/pin sessions message: {e}")

# ===== Main tasks =====
@tasks.loop(minutes=6)
async def update_prices():
    async with aiohttp.ClientSession() as session:
        btc_price, _ = await get_price_and_volume(session, "bitcoin")
        eth_price, _ = await get_price_and_volume(session, "ethereum")
        if btc_price is not None:
            await update_channel_if_changed(BTC_PRICE_CHANNEL_ID, f"BTC: ${btc_price:,.2f}", "btc_price")
        if eth_price is not None:
            await update_channel_if_changed(ETH_PRICE_CHANNEL_ID, f"ETH: ${eth_price:,.2f}", "eth_price")

@tasks.loop(minutes=17)
async def update_volumes():
    async with aiohttp.ClientSession() as session:
        _, btc_vol = await get_price_and_volume(session, "bitcoin")
        _, eth_vol = await get_price_and_volume(session, "ethereum")
        if btc_vol is not None:
            await update_channel_if_changed(BTC_VOL_CHANNEL_ID, f"BTC Vol: {format_volume(btc_vol)}", "btc_vol")
        if eth_vol is not None:
            await update_channel_if_changed(ETH_VOL_CHANNEL_ID, f"ETH Vol: {format_volume(eth_vol)}", "eth_vol")

@tasks.loop(minutes=43)
async def update_fng():
    async with aiohttp.ClientSession() as session:
        fng_value = await get_fear_and_greed(session)
        if fng_value is not None:
            await update_channel_if_changed(FNG_CHANNEL_ID, f"Fear & Greed: {fng_value}", "fng")

@tasks.loop(minutes=1)
async def update_sessions():
    await update_sessions_message()

@tasks.loop(minutes=9)
async def ping_health():
    if HEALTH_URL:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(HEALTH_URL, timeout=5) as resp:
                    if resp.status == 200:
                        logger.info("✅ HEALTH URL pinged")
                    else:
                        logger.warning(f"⚠️ HEALTH ping returned status {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ HEALTH ping error: {e}")

@bot.event
async def on_ready():
    logger.info(f"✅ Bot started as {bot.user}")
    update_prices.start()
    update_volumes.start()
    update_fng.start()
    update_sessions.start()
    ping_health.start()

bot.run(DISCORD_TOKEN)

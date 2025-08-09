import os
import logging
import asyncio
import aiohttp
import discord
from discord.ext import tasks, commands
from flask import Flask
from threading import Thread
from datetime import datetime, timezone, timedelta
import feedparser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============== CONFIG ===============
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BTC_PRICE_CHANNEL_ID = int(os.getenv("BTC_PRICE_CHANNEL_ID"))
ETH_PRICE_CHANNEL_ID = int(os.getenv("ETH_PRICE_CHANNEL_ID"))
FNG_CHANNEL_ID = int(os.getenv("FNG_CHANNEL_ID"))
BTC_VOL_CHANNEL_ID = int(os.getenv("BTC_VOL_CHANNEL_ID"))
ETH_VOL_CHANNEL_ID = int(os.getenv("ETH_VOL_CHANNEL_ID"))
SESSIONS_CHANNEL_ID = int(os.getenv("SESSIONS_CHANNEL_ID"))  # текстовый канал для сессий
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID"))          # текстовый канал для новостей
HEALTH_URL = os.getenv("HEALTH_URL")  # Для Koyeb Ping
RSS_FEED_URL = os.getenv("RSS_FEED_URL", "https://www.forexfactory.com/ffcal_week_this.xml")  # пример rss с forex factory
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

# ===== Shared state for last values to avoid redundant updates =====
last_values = {
    "btc_price": None,
    "eth_price": None,
    "btc_vol": None,
    "eth_vol": None,
    "fng": None,
    "sessions_msg_id": None,
    "sessions_last_update": None,
    "last_news_guids": set(),
}

# ===== Async HTTP fetch with retry and backoff for rate limits =====
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
                logger.info(f"Обновлен канал {channel_id}: {new_name}")
            except discord.HTTPException as e:
                logger.error(f"Ошибка обновления канала {channel_id}: {e}")

# ===== Time utils for session countdown =====
def format_relative_time(delta: timedelta) -> str:
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes = remainder // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

def format_updated_since(updated_time: datetime, now: datetime) -> str:
    diff = now - updated_time
    total_seconds = int(diff.total_seconds())
    if total_seconds < 60:
        return "обновлено только что"
    elif total_seconds < 3600:
        mins = total_seconds // 60
        return f"обновлено {mins} мин назад"
    else:
        hours = total_seconds // 3600
        return f"обновлено {hours} ч назад"

def get_session_status_emoji(status, relative):
    if status == "open":
        return "🟢"
    elif status == "closing_soon":
        return "🟡"
    else:
        return "🔴"

# ===== Market sessions data =====
# Время открытия/закрытия в UTC, как кортеж (открытие, закрытие)
MARKET_SESSIONS = {
    "Tokyo": (timedelta(hours=0), timedelta(hours=9)),      # 00:00 - 09:00 UTC (пример, уточни часы)
    "London": (timedelta(hours=8), timedelta(hours=17)),   # 08:00 - 17:00 UTC
    "New York": (timedelta(hours=13), timedelta(hours=22)),# 13:00 - 22:00 UTC
}

# Настроим более точные временные интервалы для сессий в UTC (например):
# Токио 00:00-09:00 UTC
# Лондон 08:00-17:00 UTC
# Нью-Йорк 13:00-22:00 UTC

def get_next_session_times(now_utc: datetime, open_offset: timedelta, close_offset: timedelta):
    """
    Возвращает кортеж (status, timedelta до открытия или закрытия)
    status: "open", "closed", "closing_soon"
    """
    # Приводим время до сегодняшнего начала UTC суток
    today_start = datetime(year=now_utc.year, month=now_utc.month, day=now_utc.day, tzinfo=timezone.utc)
    open_time = today_start + open_offset
    close_time = today_start + close_offset

    if now_utc < open_time:
        # Ещё не открылись сегодня
        delta_to_open = open_time - now_utc
        return "closed", delta_to_open
    elif open_time <= now_utc < close_time:
        # Открыты
        delta_to_close = close_time - now_utc
        # Если осталось менее 30 мин, помечаем "closing_soon"
        if delta_to_close <= timedelta(minutes=30):
            return "closing_soon", delta_to_close
        return "open", delta_to_close
    else:
        # Закрыты, ждём завтрашнего открытия
        next_open_time = open_time + timedelta(days=1)
        delta_to_open = next_open_time - now_utc
        return "closed", delta_to_open

# ===== Sessions message updater =====
async def update_sessions_message():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    lines = []
    for market, (open_offset, close_offset) in MARKET_SESSIONS.items():
        status, delta = get_next_session_times(now, open_offset, close_offset)
        emoji = get_session_status_emoji(status, delta)
        if status == "open":
            lines.append(f"{emoji} {market}: open — closes in {format_relative_time(delta)}")
        else:
            lines.append(f"{emoji} {market}: closed — opens in {format_relative_time(delta)}")

    last_update = last_values.get("sessions_last_update")
    if last_update:
        updated_text = format_updated_since(last_update, now)
    else:
        updated_text = f"обновлено {now.strftime('%Y-%m-%d %H:%M UTC')}"

    header = f"🕒 Market sessions (relative times, UTC) — {updated_text}"
    footer = "\n\n⚠️ Countdown is relative (D days Hh Mm). Gap alerts posted for session opens."
    full_text = header + "\n\n" + "\n".join(lines) + footer

    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not channel:
        logger.error(f"SESSIONS_CHANNEL_ID={SESSIONS_CHANNEL_ID} не найден")
        return

    # Пиннем сообщение один раз и потом редактируем его
    message_id = last_values.get("sessions_msg_id")
    message = None
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
        except Exception as e:
            logger.warning(f"Не удалось получить pinned message сессий: {e}")

    if not message:
        # Отправляем новое сообщение и закрепляем
        message = await channel.send(full_text)
        await message.pin()
        last_values["sessions_msg_id"] = message.id
    else:
        if message.content != full_text:
            await message.edit(content=full_text)

    last_values["sessions_last_update"] = now
    logger.info("Обновлено сообщение сессий")

# ===== News fetcher and notifier =====
async def fetch_and_post_news():
    now_utc = datetime.now(timezone.utc)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(RSS_FEED_URL) as resp:
                if resp.status != 200:
                    logger.warning(f"News feed unavailable: HTTP {resp.status}")
                    return
                content = await resp.text()
    except Exception as e:
        logger.error(f"Ошибка получения новостей: {e}")
        return

    feed = feedparser.parse(content)
    new_entries = []
    for entry in feed.entries:
        # Проверяем уникальность по guid или link
        guid = getattr(entry, "id", None) or getattr(entry, "link", None)
        if guid and guid not in last_values["last_news_guids"]:
            new_entries.append(entry)
            last_values["last_news_guids"].add(guid)

    if not new_entries:
        logger.info("No new news entries found")
        return

    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if not channel:
        logger.error(f"NEWS_CHANNEL_ID={NEWS_CHANNEL_ID} не найден")
        return

    # Публикуем новости (ограничение Discord: не спамить, можно брать топ 3)
    for entry in new_entries[:3]:
        title = entry.title if hasattr(entry, "title") else "No title"
        link = entry.link if hasattr(entry, "link") else ""
        published = getattr(entry, "published", "unknown time")
        msg = f"📰 **{title}**\n🕒 {published}\n🔗 {link}"
        try:
            await channel.send(msg)
        except Exception as e:
            logger.error(f"Ошибка при отправке новости: {e}")

# ===== Background tasks =====
@tasks.loop(minutes=6)
async def prices_loop():
    async with aiohttp.ClientSession() as session:
        btc_price, btc_vol = await get_price_and_volume(session, "bitcoin")
        eth_price, eth_vol = await get_price_and_volume(session, "ethereum")
        fng = await get_fear_and_greed(session)

        if btc_price is not None:
            await update_channel_if_changed(BTC_PRICE_CHANNEL_ID, f"BTC: ${btc_price:,.0f}", "btc_price")
        if eth_price is not None:
            await update_channel_if_changed(ETH_PRICE_CHANNEL_ID, f"ETH: ${eth_price:,.2f}", "eth_price")
        if btc_vol is not None:
            await update_channel_if_changed(BTC_VOL_CHANNEL_ID, f"BTC Vol: {format_volume(btc_vol)}", "btc_vol")
        if eth_vol is not None:
            await update_channel_if_changed(ETH_VOL_CHANNEL_ID, f"ETH Vol: {format_volume(eth_vol)}", "eth_vol")
        if fng is not None:
            await update_channel_if_changed(FNG_CHANNEL_ID, f"Fear & Greed: {fng}", "fng")

@tasks.loop(minutes=43)
async def fng_loop():
    async with aiohttp.ClientSession() as session:
        fng = await get_fear_and_greed(session)
        if fng is not None:
            await update_channel_if_changed(FNG_CHANNEL_ID, f"Fear & Greed: {fng}", "fng")

@tasks.loop(minutes=17)
async def volume_loop():
    async with aiohttp.ClientSession() as session:
        _, btc_vol = await get_price_and_volume(session, "bitcoin")
        _, eth_vol = await get_price_and_volume(session, "ethereum")

        if btc_vol is not None:
            await update_channel_if_changed(BTC_VOL_CHANNEL_ID, f"BTC Vol: {format_volume(btc_vol)}", "btc_vol")
        if eth_vol is not None:
            await update_channel_if_changed(ETH_VOL_CHANNEL_ID, f"ETH Vol: {format_volume(eth_vol)}", "eth_vol")

@tasks.loop(minutes=6)
async def sessions_loop():
    await update_sessions_message()

@tasks.loop(minutes=10)
async def news_loop():
    await fetch_and_post_news()

# ===== Startup =====
@bot.event
async def on_ready():
    logger.info(f"✅ Bot started as {bot.user}")

    # Запускаем циклы
    prices_loop.start()
    fng_loop.start()
    volume_loop.start()
    sessions_loop.start()
    news_loop.start()

    # Пинг URL здоровья (Koyeb) каждые 5 мин
    async def health_ping():
        while True:
            if HEALTH_URL:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(HEALTH_URL) as resp:
                            logger.info(f"Health ping status: {resp.status}")
                except Exception as e:
                    logger.warning(f"Health ping error: {e}")
            await asyncio.sleep(300)

    bot.loop.create_task(health_ping())

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

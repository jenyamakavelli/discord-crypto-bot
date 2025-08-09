import os
import logging
import asyncio
import aiohttp
import discord
from discord.ext import tasks, commands
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta, timezone
import pytz
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
SESSIONS_CHANNEL_ID = int(os.getenv("SESSIONS_CHANNEL_ID"))
ECONOMIC_NEWS_CHANNEL_ID = int(os.getenv("ECONOMIC_NEWS_CHANNEL_ID"))
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

# ===== Shared state for last values to avoid redundant updates =====
last_values = {
    "btc_price": None,
    "eth_price": None,
    "btc_vol": None,
    "eth_vol": None,
    "fng": None,
    "sessions_message_id": None,
    "news_last_published": None,
    "gap_alerts_posted": set(),
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

# ===== Formatting helpers =====
def format_volume(vol):
    if vol >= 1_000_000_000:
        return f"${vol/1_000_000_000:.1f}B"
    elif vol >= 1_000_000:
        return f"${vol/1_000_000:.1f}M"
    else:
        return f"${vol:,.0f}"

def format_timedelta_rel(td: timedelta):
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        return "0m"
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

def format_updated_since(updated_dt: datetime, now: datetime):
    diff = now - updated_dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "обновлено только что"
    elif seconds < 3600:
        mins = seconds // 60
        return f"обновлено {mins} мин назад"
    else:
        hours = seconds // 3600
        return f"обновлено {hours} ч назад"

# ===== Forex Market sessions logic =====
# Майами timezone
MIAMI_TZ = pytz.timezone("America/New_York")  # EDT/EST

# Основные сессии (время открытия и закрытия в часах UTC)
SESSIONS = {
    "Tokyo": {"start_utc": 0, "end_utc": 9},     # 00:00-09:00 UTC (примерно)
    "London": {"start_utc": 8, "end_utc": 17},  # 08:00-17:00 UTC
    "New York": {"start_utc": 13, "end_utc": 22} # 13:00-22:00 UTC
}

def is_market_open(now_utc: datetime):
    # Форекс открыт с воскресенья 17:00 Майами (UTC-4) до пятницы 17:00 Майами
    now_miami = now_utc.astimezone(MIAMI_TZ)
    weekday = now_miami.weekday()  # 0=пн,6=вс
    hour = now_miami.hour
    # Воскресенье
    if weekday == 6:
        if hour < 17:
            return False
        else:
            return True
    # Пятница
    elif weekday == 4:
        if hour >= 17:
            return False
        else:
            return True
    # Суббота — закрыто
    elif weekday == 5:
        return False
    # Будни кроме пятницы
    else:
        return True

def session_status_and_time(now_utc: datetime, session_name: str):
    # Вернёт словарь с ключами:
    # status: "open" | "closed"
    # relative: timedelta до открытия или закрытия
    # is_soon: bool (если сессия откроется менее чем через 1 час)
    sess = SESSIONS[session_name]
    start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=sess["start_utc"])
    end = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=sess["end_utc"])
    # Если время прошло, значит это сегодняшняя сессия или следующая
    if now_utc < start:
        status = "closed"
        rel = start - now_utc
    elif start <= now_utc < end:
        status = "open"
        rel = end - now_utc
    else:
        # Уже после окончания сессии сегодня — считаем следующую сессию завтра
        status = "closed"
        rel = (start + timedelta(days=1)) - now_utc

    # Если скоро открывается (менее часа)
    is_soon = False
    if status == "closed" and rel <= timedelta(hours=1):
        is_soon = True
    return {
        "status": status,
        "relative": rel,
        "is_soon": is_soon
    }

def get_session_status_emoji(status: str, is_soon: bool):
    if status == "open":
        return "🟢"
    elif status == "closed" and is_soon:
        return "🟡"
    else:
        return "🔴"

# ===== Gap detection logic for 5 major pairs =====
# Пары для гэпов
GAP_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"]

# Время открытия рынка в Майами воскресенье 17:00
def get_next_market_open_miami(now_utc: datetime) -> datetime:
    now_miami = now_utc.astimezone(MIAMI_TZ)
    # Определяем дату и время ближайшего воскресенья 17:00
    days_ahead = (6 - now_miami.weekday()) % 7
    next_sunday = (now_miami + timedelta(days=days_ahead)).replace(hour=17, minute=0, second=0, microsecond=0)
    if now_miami >= next_sunday:
        next_sunday += timedelta(days=7)
    return next_sunday.astimezone(timezone.utc)

async def fetch_gap_data(session, pair):
    # Исторические цены для пары из yfinance или другого API
    # Для примера используем YahooFinance через yfinance библиотеку, но без внешних вызовов, используем aiohttp
    # Поскольку yfinance не подходит асинхронно - делаем упрощённо через публичный API
    # Тут можно заменить на реальный API с историей цены
    base = pair[:3]
    quote = pair[3:]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{base}{quote}=X?interval=1d&range=7d"
    data = await fetch_json(session, url)
    if not data:
        return None
    try:
        chart = data["chart"]["result"][0]
        timestamps = chart["timestamp"]
        closes = chart["indicators"]["quote"][0]["close"]
        # Последний закрытый день — пятница
        # Первый день после выхода — воскресенье (открытие рынка)
        # Рассчитаем гэп между закрытием пятницы и открытием воскресенья (первым закрытием после пятницы)
        if len(closes) < 2:
            return None
        gap = closes[-1] - closes[-2]
        gap_pct = gap / closes[-2] * 100
        return {
            "pair": pair,
            "gap_value": gap,
            "gap_pct": gap_pct,
            "close_before": closes[-2],
            "close_after": closes[-1]
        }
    except Exception as e:
        logger.warning(f"Error parsing gap data for {pair}: {e}")
        return None

async def gap_scan_and_post():
    async with aiohttp.ClientSession() as session:
        gaps = []
        for pair in GAP_PAIRS:
            gap_info = await fetch_gap_data(session, pair)
            if gap_info:
                gaps.append(gap_info)
        if not gaps:
            logger.info("No gap data available")
            return
        # Фильтруем по порогу (например, > 0.1% гэп)
        significant_gaps = [g for g in gaps if abs(g["gap_pct"]) >= 0.1]
        if not significant_gaps:
            logger.info("No significant gaps to post")
            return
        # Формируем сообщение с вероятностями (примитивная статистика, например, 70% перекрывается)
        msg = "**⚖️ Gap scan on market open (5 pairs):**\n"
        for g in significant_gaps:
            direction = "⬆️" if g["gap_value"] > 0 else "⬇️"
            # Для примера вероятность 70% для гэпов < 1%, 50% для > 1%
            prob = "70%" if abs(g["gap_pct"]) < 1 else "50%"
            msg += f"{g['pair']}: {direction} {g['gap_pct']:.2f}% gap, close before: {g['close_before']:.5f}, after: {g['close_after']:.5f}, prob close gap: {prob}\n"
        channel = bot.get_channel(SESSIONS_CHANNEL_ID)
        if channel:
            await channel.send(msg)
            # Отметим, что гэп-алерты для этой сессии уже были
            last_values["gap_alerts_posted"].add(datetime.utcnow().date())
            logger.info("Posted gap alerts")

# ===== Economic calendar and news parser =====
RSS_URL = "https://www.forexfactory.com/ffcal_week_this.xml"

async def fetch_and_post_news():
    async with aiohttp.ClientSession() as session:
        # Используем feedparser синхронно внутри async executor
        def parse_rss():
            return feedparser.parse(RSS_URL)
        loop = asyncio.get_running_loop()
        feed = await loop.run_in_executor(None, parse_rss)
        if not feed or not feed.entries:
            logger.warning("No news entries found")
            return

        channel = bot.get_channel(ECONOMIC_NEWS_CHANNEL_ID)
        if not channel:
            return

        now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
        new_items = []
        for entry in feed.entries:
            # Проверяем дату выхода новости (парсим pubDate)
            if "published_parsed" not in entry:
                continue
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            # Публикуем новости с высокой важностью (High impact)
            # Для примера отфильтруем по слову "High"
            title = entry.title
            if "High" not in title:
                continue
            # Публикуем новости, которые не публиковали ранее
            if last_values.get("news_last_published") and published <= last_values["news_last_published"]:
                continue
            new_items.append((published, title, entry.link))

        # Сортируем по времени публикации
        new_items.sort(key=lambda x: x[0])
        for published, title, link in new_items:
            msg = f"📢 **High impact news** at {published.strftime('%Y-%m-%d %H:%M UTC')}\n{title}\n{link}"
            await channel.send(msg)
            last_values["news_last_published"] = published
            logger.info(f"Posted news: {title}")

# ===== Discord update tasks =====
@tasks.loop(minutes=6)
async def update_prices():
    async with aiohttp.ClientSession() as session:
        btc_price, btc_vol = await get_price_and_volume(session, "bitcoin")
        eth_price, eth_vol = await get_price_and_volume(session, "ethereum")
        fng = await get_fear_and_greed(session)

        if btc_price and btc_price != last_values["btc_price"]:
            ch = bot.get_channel(BTC_PRICE_CHANNEL_ID)
            if ch:
                await ch.edit(name=f"BTC: ${btc_price:,.0f}")
                last_values["btc_price"] = btc_price

        if eth_price and eth_price != last_values["eth_price"]:
            ch = bot.get_channel(ETH_PRICE_CHANNEL_ID)
            if ch:
                await ch.edit(name=f"ETH: ${eth_price:,.2f}")
                last_values["eth_price"] = eth_price

        if btc_vol and btc_vol != last_values["btc_vol"]:
            ch = bot.get_channel(BTC_VOL_CHANNEL_ID)
            if ch:
                await ch.edit(name=f"BTC Vol: {format_volume(btc_vol)}")
                last_values["btc_vol"] = btc_vol

        if eth_vol and eth_vol != last_values["eth_vol"]:
            ch = bot.get_channel(ETH_VOL_CHANNEL_ID)
            if ch:
                await ch.edit(name=f"ETH Vol: {format_volume(eth_vol)}")
                last_values["eth_vol"] = eth_vol

        if fng is not None and fng != last_values["fng"]:
            ch = bot.get_channel(FNG_CHANNEL_ID)
            if ch:
                await ch.edit(name=f"Fear & Greed: {fng}")
                last_values["fng"] = fng

@tasks.loop(minutes=43)
async def update_fng():
    # FNG индекс обновляется реже, можно дублировать из update_prices или сделать отдельный запрос, здесь для примера нет
    pass

@tasks.loop(minutes=17)
async def update_volumes():
    # Можно вынести в update_prices с частотой 6 мин, для примера оставлено отдельно
    pass

@tasks.loop(minutes=10)
async def update_sessions():
    await update_sessions_message()

async def update_sessions_message():
    channel = bot.get_channel(SESSIONS_CHANNEL_ID)
    if channel is None:
        logger.warning("Sessions channel not found")
        return
    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    now_miami = now_utc.astimezone(MIAMI_TZ)
    last_update_dt = last_values.get("sessions_last_update", now_utc)
    updated_text = format_updated_since(last_update_dt, now_utc)

    market_open = is_market_open(now_utc)

    text = f"🕒 Market sessions (relative times, UTC) — {updated_text}\n\n"

    for session_name in ["Tokyo", "London", "New York"]:
        info = session_status_and_time(now_utc, session_name)
        emoji = get_session_status_emoji(info["status"], info["is_soon"])
        status_str = "open" if info["status"] == "open" else "closed"
        if status_str == "closed":
            text += f"{emoji} {session_name}: closed — opens in {format_timedelta_rel(info['relative'])}\n"
        else:
            text += f"{emoji} {session_name}: open — closes in {format_timedelta_rel(info['relative'])}\n"

    text += "\n⚠️ Countdown is relative (D days Hh Mm). Gap alerts posted for session opens."

    # Send or edit pinned message in channel
    if last_values.get("sessions_message_id"):
        try:
            msg = await channel.fetch_message(last_values["sessions_message_id"])
            await msg.edit(content=text)
        except discord.NotFound:
            msg = await channel.send(text)
            last_values["sessions_message_id"] = msg.id
    else:
        msg = await channel.send(text)
        last_values["sessions_message_id"] = msg.id

    last_values["sessions_last_update"] = now_utc

# ===== Background loop for gap alerts on market open (Sunday 17:00 Miami) =====
@tasks.loop(minutes=10)
async def gap_alert_check_loop():
    now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    now_miami = now_utc.astimezone(MIAMI_TZ)
    weekday = now_miami.weekday()
    hour = now_miami.hour

    # Проверяем, что сейчас воскресенье после 17:00 Майами и еще не отправляли сегодня
    if weekday == 6 and hour >= 17:
        today = now_utc.date()
        if today not in last_values["gap_alerts_posted"]:
            await gap_scan_and_post()
    # Сб и прочие дни не постим

# ===== Background loop for economic news =====
@tasks.loop(minutes=10)
async def economic_news_loop():
    await fetch_and_post_news()

@bot.event
async def on_ready():
    logger.info(f"✅ Bot started as {bot.user}")
    update_prices.start()
    update_sessions.start()
    gap_alert_check_loop.start()
    economic_news_loop.start()

    # Каналы обновляются с разной частотой, здесь по умолчанию

# Запуск Flask и Discord бота
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

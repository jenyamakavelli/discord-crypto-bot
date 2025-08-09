#!/usr/bin/env python3
# main.py — unified bot: prices/volumes/FNG + forex gap-scan + countdown + econ calendar + health ping
# Требования (requirements.txt): discord.py, aiohttp, yfinance, feedparser, pandas, python-dotenv (локально)
# Установи ENV: DISCORD_TOKEN, BTC_PRICE_CHANNEL_ID, ETH_PRICE_CHANNEL_ID, BTC_VOL_CHANNEL_ID,
# FNG_CHANNEL_ID, SESSIONS_CHANNEL_ID, GAP_ALERT_CHANNEL_ID, ECON_CAL_CHANNEL_ID, HEALTH_URL (Koyeb)

import os
import json
import logging
import asyncio
from datetime import datetime, time, timedelta, timezone

import aiohttp
import feedparser
import yfinance as yf
import pandas as pd
import discord
from discord.ext import tasks, commands
from flask import Flask
from threading import Thread

# ---------------- CONFIG ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=getattr(logging, LOG_LEVEL))
logger = logging.getLogger("main")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    logger.error("DISCORD_TOKEN not set")
    raise SystemExit(1)

# Channel IDs (env)
def env_int(name, required=True):
    v = os.getenv(name)
    if required and not v:
        logger.error("Missing env %s", name)
        raise SystemExit(1)
    return int(v) if v else None

BTC_PRICE_CHANNEL_ID = env_int("BTC_PRICE_CHANNEL_ID")
ETH_PRICE_CHANNEL_ID = env_int("ETH_PRICE_CHANNEL_ID")
FNG_CHANNEL_ID = env_int("FNG_CHANNEL_ID")
BTC_VOL_CHANNEL_ID = env_int("BTC_VOL_CHANNEL_ID")
ETH_VOL_CHANNEL_ID = env_int("ETH_VOL_CHANNEL_ID")

SESSIONS_CHANNEL_ID = env_int("SESSIONS_CHANNEL_ID")
GAP_ALERT_CHANNEL_ID = env_int("GAP_ALERT_CHANNEL_ID")
ECON_CAL_CHANNEL_ID = env_int("ECON_CAL_CHANNEL_ID")

HEALTH_URL = os.getenv("HEALTH_URL")  # optional but recommended for Koyeb

# Forex pairs default
FX_PAIRS = os.getenv("FX_PAIRS", "EURUSD=X,GBPUSD=X,USDJPY=X,AUDUSD=X,USDCAD=X").split(",")

# Econ RSS feed (default faireconomy)
ECON_RSS_URL = os.getenv("ECON_RSS_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.xml")

# Tuning params
GAP_WINDOW_MINUTES = int(os.getenv("GAP_WINDOW_MINUTES", "15"))
GAP_THRESHOLD_PCT = float(os.getenv("GAP_THRESHOLD_PCT", "0.2"))
HIST_DAYS = int(os.getenv("HIST_DAYS", "90"))
HIST_SAMPLE_LIMIT = int(os.getenv("HIST_SAMPLE_LIMIT", "60"))

PRICE_INTERVAL_MIN = int(os.getenv("PRICE_INTERVAL_MIN", "6"))   # <- 6 min
VOL_INTERVAL_MIN = int(os.getenv("VOL_INTERVAL_MIN", "17"))      # <- 17 min
FNG_INTERVAL_MIN = int(os.getenv("FNG_INTERVAL_MIN", "43"))      # <- 43 min

COUNTDOWN_INTERVAL_SEC = int(os.getenv("COUNTDOWN_INTERVAL_SEC", "60"))
OPEN_CHECK_INTERVAL_SEC = int(os.getenv("OPEN_CHECK_INTERVAL_SEC", "30"))
ECON_POLL_MIN = int(os.getenv("ECON_POLL_MIN", "5"))
HEALTH_PING_MIN = int(os.getenv("HEALTH_PING_MIN", "4"))

MIN_CHANNEL_UPDATE_INTERVAL = int(os.getenv("MIN_CHANNEL_UPDATE_INTERVAL", "600"))  # 10 min minimal channel rename interval

STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Sessions (UTC times)
SESSIONS = {
    "Tokyo": {"open": time(0, 0), "close": time(9, 0)},
    "London": {"open": time(7, 0), "close": time(16, 0)},
    "NewYork": {"open": time(12, 0), "close": time(21, 0)},
}

# --------------- Discord bot & Flask health ---------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

app = Flask(__name__)
@app.route("/")
def home():
    return "OK"

def run_flask():
    # Flask simple server for Koyeb health check
    app.run(host="0.0.0.0", port=8000)

Thread(target=run_flask, daemon=True).start()

# --------------- State persistence (async-safe) ---------------
_state_lock = asyncio.Lock()
_default_state = {
    "last_values": {"btc_price": None, "eth_price": None, "btc_vol": None, "eth_vol": None, "fng": None},
    "last_channel_update": {},  # channel_id -> unix ts
    "session_last_run": {},     # session_name -> date string
    "econ_alerts_sent": []      # list of alert ids
}

def load_state_sync():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
                # ensure keys
                for k, v in _default_state.items():
                    if k not in s:
                        s[k] = v
                return s
    except Exception as e:
        logger.warning("load_state error: %s", e)
    return dict(_default_state)

def save_state_sync(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("save_state error: %s", e)

_state = load_state_sync()

async def update_state(fn):
    async with _state_lock:
        fn(_state)
        # persist in thread
        await asyncio.to_thread(save_state_sync, _state)

# --------------- Utilities ---------------
def utc_now():
    return datetime.now(timezone.utc)

def combine_utc(date_obj, time_obj):
    return datetime.combine(date_obj, time_obj).replace(tzinfo=timezone.utc)

def human_td(td: timedelta):
    if td.total_seconds() <= 0:
        return "0m"
    s = int(td.total_seconds())
    h = s // 3600
    m = (s % 3600) // 60
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"

def format_volume(v: float) -> str:
    try:
        v = float(v)
    except Exception:
        return "N/A"
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    return f"${v:,.0f}"

# --------------- HTTP fetch helper ---------------
async def fetch_json(session: aiohttp.ClientSession, url: str, max_retries=4):
    backoff = 1
    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=12) as resp:
                if resp.status == 429:
                    ra = resp.headers.get("Retry-After")
                    wait = int(ra) if ra and ra.isdigit() else backoff
                    logger.warning("429 from %s, waiting %s", url, wait)
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, 60)
                    continue
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("HTTP error %s on %s attempt %s", e, url, attempt + 1)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    logger.error("Failed to fetch %s after %s attempts", url, max_retries)
    return None

# --------------- CoinGecko & FNG fetchers ---------------
async def get_price_and_volume(session: aiohttp.ClientSession, coin_id: str):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
    data = await fetch_json(session, url)
    if not data:
        return None, None
    try:
        price = data["market_data"]["current_price"]["usd"]
        vol = data["market_data"]["total_volume"]["usd"]
        return price, vol
    except Exception:
        logger.warning("Malformed CoinGecko data for %s", coin_id)
        return None, None

async def get_fng(session: aiohttp.ClientSession):
    url = "https://api.alternative.me/fng/"
    data = await fetch_json(session, url)
    if not data:
        return None
    try:
        return int(data["data"][0]["value"])
    except Exception:
        logger.warning("Malformed FNG data")
        return None

# --------------- Channel update (min interval guard) ---------------
async def channel_can_update(channel_id: int) -> bool:
    last = _state.get("last_channel_update", {}).get(str(channel_id), 0)
    return (int(utc_now().timestamp()) - last) >= MIN_CHANNEL_UPDATE_INTERVAL

async def mark_channel_updated(channel_id: int):
    async def fn(s):
        s.setdefault("last_channel_update", {})[str(channel_id)] = int(utc_now().timestamp())
    await update_state(fn)

async def update_channel_if_changed(channel_id: int, new_name: str, key: str):
    # compare cached value in state, update only if changed and min interval passed
    if _state.get("last_values", {}).get(key) == new_name:
        return
    if not await channel_can_update(channel_id):
        logger.debug("Skipping name edit for %s due to min interval", channel_id)
        return
    ch = bot.get_channel(channel_id)
    if not ch:
        logger.warning("Channel %s not found", channel_id)
        return
    try:
        await ch.edit(name=new_name)
        logger.info("Updated channel %s -> %s", channel_id, new_name)
        async def fn(s):
            s.setdefault("last_values", {})[key] = new_name
            s.setdefault("last_channel_update", {})[str(channel_id)] = int(utc_now().timestamp())
        await update_state(fn)
    except discord.HTTPException as e:
        # If rate-limited, discord.py retries internally but still log
        logger.warning("Discord HTTP error editing channel %s: %s", channel_id, e)
    except Exception as e:
        logger.exception("Unexpected error editing channel")

# --------------- Price / Volume / FNG loops ---------------
@tasks.loop(minutes=PRICE_INTERVAL_MIN)
async def price_loop():
    async with aiohttp.ClientSession() as session:
        btc_price, _ = await get_price_and_volume(session, "bitcoin")
        eth_price, _ = await get_price_and_volume(session, "ethereum")
        if btc_price is not None:
            await update_channel_if_changed(BTC_PRICE_CHANNEL_ID, f"BTC: ${btc_price:,.2f}", "btc_price")
        if eth_price is not None:
            await update_channel_if_changed(ETH_PRICE_CHANNEL_ID, f"ETH: ${eth_price:,.2f}", "eth_price")

@tasks.loop(minutes=VOL_INTERVAL_MIN)
async def volume_loop():
    async with aiohttp.ClientSession() as session:
        _, btc_vol = await get_price_and_volume(session, "bitcoin")
        _, eth_vol = await get_price_and_volume(session, "ethereum")
        if btc_vol is not None:
            await update_channel_if_changed(BTC_VOL_CHANNEL_ID, f"BTC Vol: {format_volume(btc_vol)}", "btc_vol")
        if eth_vol is not None:
            await update_channel_if_changed(ETH_VOL_CHANNEL_ID, f"ETH Vol: {format_volume(eth_vol)}", "eth_vol")

@tasks.loop(minutes=FNG_INTERVAL_MIN)
async def fng_loop():
    async with aiohttp.ClientSession() as session:
        fng = await get_fng(session)
        if fng is not None:
            await update_channel_if_changed(FNG_CHANNEL_ID, f"Fear & Greed: {fng}", "fng")

# --------------- Health ping ---------------
@tasks.loop(minutes=HEALTH_PING_MIN)
async def health_ping_loop():
    if not HEALTH_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(HEALTH_URL, timeout=5) as resp:
                if resp.status == 200:
                    logger.debug("Health ping OK")
                else:
                    logger.warning("Health ping returned %s", resp.status)
    except Exception as e:
        logger.warning("Health ping error: %s", e)

# --------------- yfinance helper via asyncio.to_thread ---------------
def _yf_download_sync(ticker, start, end, interval="1m"):
    try:
        df = yf.download(tickers=ticker, start=start, end=end, interval=interval, progress=False, threads=False)
        return df
    except Exception as e:
        logger.warning("yfinance download exception %s %s", ticker, e)
        return None

async def yf_download(ticker, start, end, interval="1m"):
    return await asyncio.to_thread(_yf_download_sync, ticker, start, end, interval)

def ensure_index_utc(df: pd.DataFrame):
    if df is None or df.empty:
        return df
    try:
        idx = df.index
        if getattr(idx, "tz", None) is None:
            # treat as UTC
            df.index = df.index.tz_localize(timezone.utc)
        else:
            df.index = df.index.tz_convert(timezone.utc)
    except Exception:
        pass
    return df

# --------------- Gap detection ---------------
async def gap_for_session_and_pair(pair_ticker: str, session_open_dt: datetime):
    prev_close_dt = session_open_dt - timedelta(minutes=1)
    window_end = session_open_dt + timedelta(minutes=GAP_WINDOW_MINUTES)
    start = (prev_close_dt - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    end = (window_end + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    df = await yf_download(pair_ticker, start, end, interval="1m")
    if df is None or df.empty:
        return None
    df = ensure_index_utc(df)
    prev_rows = df[df.index <= prev_close_dt]
    if prev_rows.empty:
        return None
    prev_close = float(prev_rows.iloc[-1]["Close"])
    after_rows = df[df.index >= session_open_dt]
    if after_rows.empty:
        return None
    open_price = float(after_rows.iloc[0]["Open"])
    gap_pct = (open_price - prev_close) / prev_close * 100.0
    return {"pair": pair_ticker, "prev_close": prev_close, "open_price": open_price, "gap_pct": gap_pct}

async def historical_gap_closure_probability(pair_ticker: str, session_open_time: time, sample_days=HIST_SAMPLE_LIMIT):
    now = utc_now()
    total_relevant = 0
    closed_count = 0
    checked = 0
    for d in range(1, sample_days + 1):
        day = (now - timedelta(days=d)).date()
        open_dt = combine_utc(day, session_open_time)
        prev_close_dt = open_dt - timedelta(minutes=1)
        start = (prev_close_dt - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        end = (open_dt + timedelta(hours=24, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        df = await yf_download(pair_ticker, start, end, interval="1m")
        if df is None or df.empty:
            continue
        df = ensure_index_utc(df)
        prev_rows = df[df.index <= prev_close_dt]
        after_rows = df[df.index >= open_dt]
        if prev_rows.empty or after_rows.empty:
            continue
        prev_close = float(prev_rows.iloc[-1]["Close"])
        open_price = float(after_rows.iloc[0]["Open"])
        gap_pct = (open_price - prev_close) / prev_close * 100.0
        if abs(gap_pct) < GAP_THRESHOLD_PCT:
            continue
        total_relevant += 1
        period_df = df[(df.index > open_dt) & (df.index <= open_dt + timedelta(hours=24))]
        if period_df.empty:
            continue
        if gap_pct > 0:
            crossed = (period_df["Low"] <= prev_close).any()
        else:
            crossed = (period_df["High"] >= prev_close).any()
        if crossed:
            closed_count += 1
        checked += 1
        if checked >= 60 and total_relevant >= 30:
            break
    if total_relevant == 0:
        return None
    return closed_count / total_relevant

async def post_gap_alerts(session_name: str, session_open_dt: datetime, pairs_results, probabilities):
    ch = bot.get_channel(GAP_ALERT_CHANNEL_ID)
    if not ch:
        logger.warning("Gap alert channel missing")
        return
    ts = session_open_dt.strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"🐋 **GAP SCAN — {session_name} open ({ts})**"]
    for r in pairs_results:
        gap = r["gap_pct"]
        pair = r["pair"]
        prob = probabilities.get(pair)
        arrow = "🔺" if gap > 0 else "🔻"
        lines.append(f"**{pair}** {arrow} {gap:+.3f}%  (prev {r['prev_close']:.5f} → open {r['open_price']:.5f})" +
                     (f" — Prob close in 24h: {prob*100:.1f}%" if prob is not None else " — Prob: N/A"))
    await ch.send("\n".join(lines))

async def run_gap_scans_for_session(session_name: str, session_open_dt: datetime):
    results = []
    for pair in FX_PAIRS:
        try:
            r = await gap_for_session_and_pair(pair, session_open_dt)
        except Exception as e:
            logger.warning("gap error %s %s", pair, e)
            r = None
        if r:
            results.append(r)
    probs = {}
    for r in results:
        p = await historical_gap_closure_probability(r["pair"], SESSIONS[session_name]["open"], sample_days=HIST_DAYS)
        probs[r["pair"]] = p
    await post_gap_alerts(session_name, session_open_dt, results, probs)

# --------------- Countdown pinned message ---------------
_pinned_msg_id = None

async def ensure_pinned_message():
    global _pinned_msg_id
    ch = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not ch:
        logger.warning("Sessions channel missing")
        return None
    try:
        pins = await ch.pins()
    except Exception:
        pins = []
    if pins:
        _pinned_msg_id = pins[0].id
        return pins[0]
    msg = await ch.send("Initializing sessions countdown...")
    try:
        await msg.pin()
    except Exception:
        pass
    _pinned_msg_id = msg.id
    return msg

async def update_countdown_message():
    ch = bot.get_channel(SESSIONS_CHANNEL_ID)
    if not ch:
        return
    msg = None
    if _pinned_msg_id:
        try:
            msg = await ch.fetch_message(_pinned_msg_id)
        except Exception:
            msg = None
    if not msg:
        msg = await ensure_pinned_message()
        if not msg:
            return
    now = utc_now()
    parts = [f"🕒 **Market sessions (relative times, UTC)** — updated {now.strftime('%Y-%m-%d %H:%M UTC')}\n"]
    for name, t in SESSIONS.items():
        open_dt = combine_utc(now.date(), t["open"])
        close_dt = combine_utc(now.date(), t["close"])
        if now < open_dt:
            status = f"closed — opens in {human_td(open_dt - now)}"
        elif now > close_dt:
            next_open = open_dt + timedelta(days=1)
            status = f"closed — opens in {human_td(next_open - now)}"
        else:
            status = f"open — closes in {human_td(close_dt - now)}"
        parts.append(f"**{name}**: {status}")
    parts.append("\n⚠️ Countdown is relative (Hh Mm). Gap alerts posted for session opens.")
    content = "\n".join(parts)
    try:
        await msg.edit(content=content)
    except Exception as e:
        logger.warning("Edit countdown msg failed: %s", e)

# --------------- Session open checker ---------------
async def check_session_opens():
    now = utc_now()
    for name, t in SESSIONS.items():
        open_dt = combine_utc(now.date(), t["open"])
        window_start = open_dt
        window_end = open_dt + timedelta(minutes=GAP_WINDOW_MINUTES)
        date_key = open_dt.date().isoformat()
        last_run = _state.get("session_last_run", {}).get(name)
        if window_start <= now <= window_end:
            if last_run == date_key:
                continue
            async def fn(s):
                s.setdefault("session_last_run", {})[name] = date_key
            await update_state(fn)
            logger.info("Session %s opened at %s, launching gap scan", name, open_dt.isoformat())
            asyncio.create_task(run_gap_scans_for_session(name, open_dt))

# --------------- Economic calendar (RSS) ---------------
_sent_econ = set(_state.get("econ_alerts_sent", []))

async def poll_econ_rss():
    try:
        feed = await asyncio.to_thread(feedparser.parse, ECON_RSS_URL)
    except Exception as e:
        logger.warning("RSS fetch error %s", e)
        return
    if not getattr(feed, "entries", None):
        return
    now = utc_now()
    for entry in feed.entries:
        eid = entry.get("id") or entry.get("link") or entry.get("title")
        if not eid:
            continue
        pp = entry.get("published_parsed")
        if not pp:
            continue
        event_dt = datetime(*pp[:6], tzinfo=timezone.utc)
        if event_dt <= now:
            continue
        title = entry.get("title", "")
        summary = entry.get("summary", "")
        impact = "low"
        if "High" in title or "High" in summary or "high impact" in (summary or "").lower():
            impact = "high"
        if impact != "high":
            continue
        for minutes_before in (60, 30, 10):
            run_at = event_dt - timedelta(minutes=minutes_before)
            if run_at <= now:
                continue
            alert_id = f"{eid}|{minutes_before}"
            if alert_id in _sent_econ:
                continue
            _sent_econ.add(alert_id)
            async def persist(s):
                lst = s.setdefault("econ_alerts_sent", [])
                if alert_id not in lst:
                    lst.append(alert_id)
            await update_state(persist)
            asyncio.create_task(schedule_econ_alert(run_at, ECON_CAL_CHANNEL_ID, minutes_before, title, event_dt, entry.get("link")))
            logger.info("Scheduled econ alert %s for %s", alert_id, title)

async def schedule_econ_alert(run_at: datetime, channel_id: int, minutes_before: int, title: str, event_dt: datetime, link: str):
    now = utc_now()
    to_sleep = (run_at - now).total_seconds()
    if to_sleep > 0:
        await asyncio.sleep(to_sleep)
    ch = bot.get_channel(channel_id)
    if not ch:
        logger.warning("Econ channel missing")
        return
    human = human_td(event_dt - utc_now())
    text = (f"🔔 **Economic event upcoming** — {title}\n"
            f"• In: {human}\n"
            f"• At (UTC): {event_dt.strftime('%Y-%m-%d %H:%M')}\n"
            f"• Reminder: {minutes_before} minutes before\n"
            f"{link or ''}")
    try:
        await ch.send(text)
    except Exception as e:
        logger.warning("Failed to send econ alert: %s", e)

# --------------- Tasks loops ---------------
@tasks.loop(seconds=COUNTDOWN_INTERVAL_SEC)
async def countdown_task():
    try:
        await update_countdown_message()
    except Exception as e:
        logger.exception("countdown error: %s", e)

@tasks.loop(seconds=OPEN_CHECK_INTERVAL_SEC)
async def open_check_task():
    try:
        await check_session_opens()
    except Exception as e:
        logger.exception("open_check error: %s", e)

@tasks.loop(minutes=ECON_POLL_MIN)
async def econ_poll_task():
    try:
        await poll_econ_rss()
    except Exception as e:
        logger.exception("econ poll error: %s", e)

# --------------- Bot events ---------------
@bot.event
async def on_ready():
    logger.info("Bot started as %s", bot.user)
    # ensure pinned message exists
    await ensure_pinned_message()
    # start loops
    if not price_loop.is_running():
        price_loop.start()
    if not volume_loop.is_running():
        volume_loop.start()
    if not fng_loop.is_running():
        fng_loop.start()
    if not countdown_task.is_running():
        countdown_task.start()
    if not open_check_task.is_running():
        open_check_task.start()
    if not econ_poll_task.is_running():
        econ_poll_task.start()
    if not health_ping_loop.is_running():
        health_ping_loop.start()
    logger.info("All tasks started")

# --------------- Shutdown ---------------
async def _shutdown():
    logger.info("Shutting down... saving state")
    await asyncio.to_thread(save_state_sync, _state)

# --------------- Run ---------------
if __name__ == "__main__":
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        # attempt to persist
        try:
            asyncio.run(_shutdown())
        except Exception:
            pass

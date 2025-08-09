# custom_fng.py
# Прототип кастомного Fear&Greed Index (реальное время).
# Источники: CoinGecko (обязательно), Twitter optional, Google Trends optional.
# Интервал обновления: 5 минут (по умолчанию). Меняй в TASK_INTERVAL_MIN.

import os
import math
import time
import asyncio
import logging
from statistics import mean, stdev
from typing import Optional, Dict, List

import aiohttp
import discord
from discord.ext import tasks, commands

# Optional: pytrends (pip install pytrends) for Google Trends
try:
    from pytrends.request import TrendReq
    PYTRENDS_AVAILABLE = True
except Exception:
    PYTRENDS_AVAILABLE = False

# ---------- CONFIG ----------
TOKEN = os.getenv("DISCORD_TOKEN")
CUSTOM_FNG_CHANNEL_ID = int(os.getenv("CUSTOM_FNG_CHANNEL_ID", "0"))  # Короткий канал (название)
FNG_ALERT_TEXT_CHANNEL_ID = int(os.getenv("FNG_ALERT_TEXT_CHANNEL_ID", "0"))  # Текстовый канал для подробностей/алертов (0 - отключён)
TWITTER_BEARER = os.getenv("TWITTER_BEARER")  # optional
ENABLE_GOOGLE_TRENDS = os.getenv("ENABLE_GOOGLE_TRENDS", "0") == "1"
TASK_INTERVAL_MIN = 5  # интервал перерасчёта индекса
MIN_CHANGE_ALERT = 7   # порог (п.п.) для алерта в текстовый канал

# Weights for components (sum 1.0)
WEIGHTS = {
    "volatility": 0.25,
    "volume":     0.25,
    "dominance":  0.15,
    "social":     0.20,
    "trends":     0.15,
}
# --------------------------------

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("custom_fng")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# State
_last_score: Optional[float] = None
_last_components: Dict[str, float] = {}

# Helper: normalize value to 0..100 given min/max
def normalize(val: float, vmin: float, vmax: float, invert: bool = False) -> float:
    if math.isnan(val) or val is None:
        return 50.0
    if vmax == vmin:
        score = 50.0
    else:
        score = (val - vmin) / (vmax - vmin) * 100.0
        score = max(0.0, min(100.0, score))
    return 100.0 - score if invert else score

# ---------- Data fetchers (async) ----------
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

async def coingecko_market_data(session: aiohttp.ClientSession, ids: List[str]) -> Optional[List[dict]]:
    """
    Fetch /coins/markets for ids (bitcoin, ethereum)
    """
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": ",".join(ids),
        "order": "market_cap_desc",
        "per_page": len(ids),
        "page": 1,
        "sparkline": "false",
    }
    try:
        async with session.get(url, params=params, timeout=20) as r:
            if r.status != 200:
                log.warning(f"CoinGecko markets status {r.status}")
                return None
            return await r.json()
    except Exception as e:
        log.warning(f"CoinGecko markets error: {e}")
        return None

async def coingecko_prices_history(session: aiohttp.ClientSession, coin_id: str, days: int = 1) -> Optional[List[List[float]]]:
    """
    /coins/{id}/market_chart?vs_currency=usd&days={days}
    returns 'prices': [[ts, price], ...]
    """
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": days, "interval": "hourly"}
    try:
        async with session.get(url, params=params, timeout=20) as r:
            if r.status != 200:
                log.warning(f"CoinGecko history {coin_id} status {r.status}")
                return None
            data = await r.json()
            return data.get("prices", [])
    except Exception as e:
        log.warning(f"CoinGecko history error {e}")
        return None

async def coingecko_global(session: aiohttp.ClientSession) -> Optional[dict]:
    url = f"{COINGECKO_BASE}/global"
    try:
        async with session.get(url, timeout=15) as r:
            if r.status != 200:
                log.warning("CoinGecko global status %s", r.status)
                return None
            return await r.json()
    except Exception as e:
        log.warning("CoinGecko global error %s", e)
        return None

# Optional: Twitter recent search (simple tweet count proxy)
async def twitter_recent_count(session: aiohttp.ClientSession, query: str = "bitcoin", minutes: int = 60) -> Optional[int]:
    if not TWITTER_BEARER:
        return None
    url = "https://api.twitter.com/2/tweets/search/recent"
    # recent search allows max_time_range ~7 days, we use 'start_time' param - but to keep simple we'll request default and count returned.
    headers = {"Authorization": f"Bearer {TWITTER_BEARER}"}
    params = {"query": query, "max_results": 100}
    try:
        async with session.get(url, headers=headers, params=params, timeout=15) as r:
            if r.status != 200:
                log.warning("Twitter API status %s", r.status)
                return None
            data = await r.json()
            count = len(data.get("data", []))
            return count
    except Exception as e:
        log.warning("Twitter error %s", e)
        return None

# Optional: Google Trends via pytrends
def google_trends_interest(keywords: List[str], timeframe: str = "now 1-H") -> Optional[float]:
    """
    Synchronous call using pytrends (if available).
    Returns aggregated interest (0..100) averaged across keywords.
    """
    if not PYTRENDS_AVAILABLE or not ENABLE_GOOGLE_TRENDS:
        return None
    try:
        pytrends = TrendReq(hl="en-US", tz=0)
        pytrends.build_payload(keywords, cat=0, timeframe=timeframe, geo="", gprop="")
        df = pytrends.interest_over_time()
        if df.empty:
            return None
        # take last row average
        last = df.iloc[-1]
        # pytrends returns columns for each keyword and 'isPartial'
        vals = [float(last[k]) for k in keywords if k in last.index]
        if not vals:
            return None
        return float(sum(vals) / len(vals))
    except Exception as e:
        log.warning("pytrends error %s", e)
        return None

# ---------- Scoring components ----------
def compute_volatility_score(prices: List[List[float]]) -> float:
    """
    prices: list of [timestamp, price], we compute log returns stddev (percent) over available points.
    Map stddev to 0..100 inverted (high vol -> fear -> low score).
    Thresholds chosen heuristically.
    """
    if not prices or len(prices) < 3:
        return 50.0
    vals = [p[1] for p in prices]
    # log returns
    returns = []
    for i in range(1, len(vals)):
        if vals[i-1] > 0 and vals[i] > 0:
            r = math.log(vals[i] / vals[i-1])
            returns.append(r)
    if not returns:
        return 50.0
    # std dev of returns * sqrt to scale to daily-ish percent
    vol = (stdev(returns) * math.sqrt(len(returns))) * 100.0  # crude scaling to %
    # map vol to score: vol 0% -> 100 (greed), vol 8%+ -> 0 (fear)
    vmin, vmax = 0.0, 8.0
    return normalize(vol, vmin, vmax, invert=True)

def compute_volume_score(current_vol: float, avg_vol: float) -> float:
    """
    current_vol relative to avg_vol.
    If current_vol >> avg_vol => greed (higher score).
    Normalize ratio into 0..100 (ratio 0.5 -> 0; ratio 1 -> 50; ratio 3+ -> 100) heuristically.
    """
    if not current_vol or not avg_vol or avg_vol <= 0:
        return 50.0
    ratio = current_vol / avg_vol
    # map ratio 0.5..3 => 0..100
    rmin, rmax = 0.5, 3.0
    return normalize(ratio, rmin, rmax, invert=False)

def compute_dominance_score(btc_dom_percent: float) -> float:
    """
    Higher BTC dominance historically sometimes signals fear (flight to BTC), so we invert:
    - high dominance -> lower score (fear)
    Map 30%..70% -> 100..0
    """
    if btc_dom_percent is None:
        return 50.0
    vmin, vmax = 30.0, 70.0
    return normalize(btc_dom_percent, vmin, vmax, invert=True)

def compute_social_score(tweet_count: Optional[int]) -> float:
    """
    More tweets -> more attention, usually greed; but spikes may be fear too.
    We'll map count to 0..100 using heuristics.
    """
    if tweet_count is None:
        return 50.0
    # heuristic: 0..1000 tweets => 0..100
    tmin, tmax = 0, 1000
    return normalize(tweet_count, tmin, tmax, invert=False)

def compute_trends_score(trends_interest: Optional[float]) -> float:
    """
    Google Trends interest: higher interest can be interpreted as fear or greed depending on query.
    We choose keywords that indicate buying intent ("buy bitcoin") -> higher interest -> greed.
    trends_interest already 0..100.
    """
    if trends_interest is None:
        return 50.0
    return max(0.0, min(100.0, trends_interest))

# ---------- Main compute function ----------
async def compute_custom_fng() -> Optional[Dict]:
    """
    Fetch data, compute sub-scores, combine into global score (0..100).
    Returns dict with score and components.
    """
    async with aiohttp.ClientSession() as session:
        # 1) CoinGecko markets for current price/volume
        markets = await coingecko_market_data(session, ["bitcoin", "ethereum"])
        if not markets or len(markets) < 1:
            log.warning("No market data from CoinGecko")
            return None
        # map markets by id
        m_by_id = {m["id"]: m for m in markets}
        btc = m_by_id.get("bitcoin")
        eth = m_by_id.get("ethereum")

        # 2) price history for volatility (BTC 24h)
        btc_history = await coingecko_prices_history(session, "bitcoin", days=1)
        # compute vol score
        vol_score = compute_volatility_score(btc_history)

        # 3) volumes: current 24h volume and 30d avg (approx)
        current_btc_vol = btc.get("total_volume") if btc else None
        # compute 30d average volume with /market_chart days=30
        hist30 = await coingecko_prices_history(session, "bitcoin", days=30)  # but this endpoint returns 'prices' only; coin endpoint for volumes is different
        # fallback: use market data 24h volume as both current and avg if no historic
        avg_btc_vol = None
        # try another endpoint for 30d volumes
        try:
            async with session.get(f"{COINGECKO_BASE}/coins/bitcoin/market_chart", params={"vs_currency": "usd", "days": 30}, timeout=20) as r30:
                if r30.status == 200:
                    d30 = await r30.json()
                    vols = [v for ts, v in d30.get("total_volumes", [])] if d30.get("total_volumes") else []
                    if vols:
                        avg_btc_vol = sum(vols) / len(vols)
        except Exception as e:
            log.debug("historic volume fetch failed: %s", e)
        if avg_btc_vol is None:
            avg_btc_vol = current_btc_vol or 1.0

        vol_score_component = compute_volume_score(current_btc_vol or 0.0, avg_btc_vol)

        # 4) dominance
        gdata = await coingecko_global(session)
        btc_dom = None
        if gdata:
            btc_dom = gdata.get("data", {}).get("market_cap_percentage", {}).get("btc")
        dom_score = compute_dominance_score(btc_dom)

        # 5) social (twitter) optional
        tweet_count = None
        if TWITTER_BEARER:
            tweet_count = await twitter_recent_count(session, query="bitcoin", minutes=60)
        social_score = compute_social_score(tweet_count)

        # 6) google trends optional (sync)
        trends_score = None
        if ENABLE_GOOGLE_TRENDS and PYTRENDS_AVAILABLE:
            # simple keywords reflecting buying interest
            try:
                trends_score = google_trends_interest(["buy bitcoin", "buy btc"], timeframe="now 1-H")
            except Exception as e:
                log.warning("Google Trends failed: %s", e)
                trends_score = None

        trends_score = compute_trends_score(trends_score)

        # combine components
        components = {
            "volatility": vol_score,
            "volume": vol_score_component,
            "dominance": dom_score,
            "social": social_score,
            "trends": trends_score,
        }

        # weighted sum
        score = sum(components[k] * WEIGHTS.get(k, 0) for k in components)
        score = max(0.0, min(100.0, score))

        return {"score": round(score, 2), "components": components, "meta": {
            "btc_price": btc.get("current_price") if btc else None,
            "btc_vol": current_btc_vol,
            "btc_dom": btc_dom,
            "tweet_count": tweet_count,
        }}

# ---------- Discord integration ----------
# update a short channel name with "CF&G: 62" or similar
async def update_custom_fng_channel(score: float):
    if not CUSTOM_FNG_CHANNEL_ID:
        return
    ch = bot.get_channel(CUSTOM_FNG_CHANNEL_ID)
    if not ch:
        log.warning("Custom FNG channel not found")
        return
    # create name, keep short
    name = f"CF&G: {int(round(score))}"
    try:
        if getattr(ch, "name", "") != name:
            await ch.edit(name=name)
            log.info("Updated custom F&G channel -> %s", name)
    except discord.HTTPException as e:
        log.warning("Discord edit channel error: %s", e)

async def post_detailed_alert(score: float, components: Dict[str, float], meta: Dict):
    """
    Post a message in text channel with breakdown when significant change occurs.
    """
    if not FNG_ALERT_TEXT_CHANNEL_ID:
        return
    try:
        ch = bot.get_channel(FNG_ALERT_TEXT_CHANNEL_ID)
        if not ch:
            return
        text = (
            f"**Custom F&G**: **{score:.2f}**\n"
            f"- Volatility: {components['volatility']:.1f}\n"
            f"- Volume: {components['volume']:.1f}\n"
            f"- Dominance: {components['dominance']:.1f}\n"
            f"- Social: {components['social']:.1f}\n"
            f"- Trends: {components['trends']:.1f}\n"
            f"Price BTC: ${meta.get('btc_price')}, 24h vol: {meta.get('btc_vol')}\n"
        )
        await ch.send(text)
    except Exception as e:
        log.warning("Failed to post alert: %s", e)

# Task loop
@tasks.loop(minutes=TASK_INTERVAL_MIN)
async def fng_task():
    global _last_score, _last_components
    try:
        res = await compute_custom_fng()
        if not res:
            log.warning("compute_custom_fng returned nothing")
            return
        score = res["score"]
        components = res["components"]
        meta = res.get("meta", {})

        # update short channel
        await update_custom_fng_channel(score)

        # if first run, set and post details
        if _last_score is None:
            _last_score = score
            _last_components = components
            # post initial detailed snapshot
            await post_detailed_alert(score, components, meta)
            return

        # significant change?
        diff = abs(score - _last_score)
        if diff >= MIN_CHANGE_ALERT:
            log.info("Significant change detected: %s -> %s (diff=%.2f)", _last_score, score, diff)
            await post_detailed_alert(score, components, meta)

        # save
        _last_score = score
        _last_components = components
    except Exception as e:
        log.exception("fng_task error: %s", e)

# ---------- bot events ----------
@bot.event
async def on_ready():
    log.info("Bot ready: %s", bot.user)
    if not fng_task.is_running():
        fng_task.start()
        log.info("Custom F&G task started (every %d min)", TASK_INTERVAL_MIN)

# ---------- run ----------
if __name__ == "__main__":
    if not TOKEN:
        log.error("DISCORD_TOKEN missing")
        raise SystemExit(1)
    bot.run(TOKEN)

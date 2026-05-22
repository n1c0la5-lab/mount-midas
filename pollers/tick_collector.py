"""
tick_collector.py — Binance Marktdaten Collector
- aggTrades (Spot + Perp) → spot_trades / perp_trades, alle 60s
- Funding Rate + Open Interest → funding_rates / open_interest, stündlich
- OHLCV täglich → ohlcv_daily, täglich 00:05 UTC + einmaliger Backfill
Rolling buffer aggTrades: 90 Tage.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta, date

import aiohttp
import psycopg

log = logging.getLogger(__name__)

SPOT_URL    = "https://api.binance.com/api/v3/aggTrades"
PERP_URL    = "https://fapi.binance.com/fapi/v1/aggTrades"
KLINES_URL  = "https://api.binance.com/api/v3/klines"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
OI_URL      = "https://fapi.binance.com/fapi/v1/openInterest"
SYMBOL      = "ICPUSDT"
PERP_SYMBOL = "ICPUSDT"
BATCH_LIMIT = 1000          # Max trades per request
RATIO_WINDOW_MIN = 60       # Perp/Spot ratio window in minutes
PERP_ALERT_RATIO = 3.0      # Threshold: "Da ist was los!"
ROLLING_DAYS = 90

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)


async def get_last_trade_id(conn, table: str) -> int | None:
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT MAX(agg_trade_id) FROM {table}")
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def fetch_trades(
    session: aiohttp.ClientSession, url: str, symbol: str, from_id: int | None
) -> list[dict]:
    params = {"symbol": symbol, "limit": BATCH_LIMIT}
    if from_id is not None:
        params["fromId"] = from_id
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json()
    except Exception as e:
        log.warning("tick fetch error %s: %s", url, e)
        return []


async def insert_trades(conn, table: str, trades: list[dict]) -> int:
    if not trades:
        return 0
    async with conn.cursor() as cur:
        for t in trades:
            await cur.execute(
                f"""
                INSERT INTO {table} (agg_trade_id, ts, price, quantity_icp, is_buyer_maker)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (agg_trade_id) DO NOTHING
                """,
                (
                    t["a"],
                    datetime.fromtimestamp(t["T"] / 1000, tz=timezone.utc),
                    float(t["p"]),
                    float(t["q"]),
                    t["m"],
                ),
            )
    await conn.commit()
    return len(trades)


async def collect_spot(session: aiohttp.ClientSession, conn) -> int:
    last_id = await get_last_trade_id(conn, "spot_trades")
    from_id = last_id + 1 if last_id else None
    trades = await fetch_trades(session, SPOT_URL, SYMBOL, from_id)
    n = await insert_trades(conn, "spot_trades", trades)
    if n:
        log.debug("spot: +%d trades (last_id=%s)", n, trades[-1]["a"] if trades else "n/a")
    return n


async def collect_perp(session: aiohttp.ClientSession, conn) -> int:
    last_id = await get_last_trade_id(conn, "perp_trades")
    from_id = last_id + 1 if last_id else None
    trades = await fetch_trades(session, PERP_URL, PERP_SYMBOL, from_id)
    n = await insert_trades(conn, "perp_trades", trades)
    if n:
        log.debug("perp: +%d trades (last_id=%s)", n, trades[-1]["a"] if trades else "n/a")
    return n


async def calc_ratio(conn) -> None:
    """Calculate perp/spot volume ratio for the last RATIO_WINDOW_MIN minutes."""
    since = datetime.now(tz=timezone.utc) - timedelta(minutes=RATIO_WINDOW_MIN)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(SUM(quantity_icp), 0) FROM spot_trades WHERE ts >= %s", (since,)
        )
        spot_vol = float((await cur.fetchone())[0])

        await cur.execute(
            "SELECT COALESCE(SUM(quantity_icp), 0) FROM perp_trades WHERE ts >= %s", (since,)
        )
        perp_vol = float((await cur.fetchone())[0])

    if spot_vol == 0:
        return

    ratio = perp_vol / spot_vol
    alert = ratio >= PERP_ALERT_RATIO

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO market_activity (ts, spot_volume_icp, perp_volume_icp, perp_spot_ratio, activity_alert)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (datetime.now(tz=timezone.utc), round(spot_vol, 2), round(perp_vol, 2), round(ratio, 4), alert),
        )
    await conn.commit()

    if alert:
        log.info("ACTIVITY ALERT: perp/spot ratio=%.2f (threshold=%.1f)", ratio, PERP_ALERT_RATIO)
    else:
        log.debug("market_activity: spot=%.0f perp=%.0f ratio=%.2f", spot_vol, perp_vol, ratio)


async def cleanup_old_trades(conn) -> None:
    """Delete trades older than ROLLING_DAYS."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=ROLLING_DAYS)
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM spot_trades WHERE ts < %s", (cutoff,))
        await cur.execute("DELETE FROM perp_trades WHERE ts < %s", (cutoff,))
    await conn.commit()
    log.info("cleanup: removed trades older than %d days", ROLLING_DAYS)


async def run() -> None:
    """Collect one batch of trades + calc ratio — called every 60s by runner."""
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        async with aiohttp.ClientSession() as session:
            await collect_spot(session, conn)
            await collect_perp(session, conn)
        await calc_ratio(conn)


async def run_cleanup() -> None:
    """Called once daily by runner."""
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        await cleanup_old_trades(conn)


async def collect_funding(session: aiohttp.ClientSession, conn) -> None:
    """Fetch current funding rate from Binance perpetuals, store hourly snapshot."""
    try:
        async with session.get(
            FUNDING_URL, params={"symbol": PERP_SYMBOL},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        rate     = float(data["lastFundingRate"])
        next_ts  = datetime.fromtimestamp(int(data["nextFundingTime"]) / 1000, tz=timezone.utc)
        mark     = float(data["markPrice"])
        ts       = datetime.now(tz=timezone.utc)
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO funding_rates (ts, funding_rate, next_funding_ts, mark_price)
                   VALUES (%s, %s, %s, %s)""",
                (ts, rate, next_ts, mark),
            )
        await conn.commit()
        log.debug("funding: rate=%.6f next=%s", rate, next_ts)
    except Exception as e:
        log.warning("funding fetch error: %s", e)


async def collect_oi(session: aiohttp.ClientSession, conn) -> None:
    """Fetch open interest snapshot from Binance perpetuals."""
    try:
        async with session.get(
            OI_URL, params={"symbol": PERP_SYMBOL},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        oi_icp = float(data["openInterest"])
        ts     = datetime.fromtimestamp(int(data["time"]) / 1000, tz=timezone.utc)
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO open_interest (ts, oi_icp) VALUES (%s, %s)",
                (ts, oi_icp),
            )
        await conn.commit()
        log.debug("oi: %.0f ICP", oi_icp)
    except Exception as e:
        log.warning("oi fetch error: %s", e)


async def collect_ohlcv(session: aiohttp.ClientSession, conn) -> None:
    """Store the last closed daily candle. Skips if date already exists."""
    try:
        async with session.get(
            KLINES_URL,
            params={"symbol": SYMBOL, "interval": "1d", "limit": 2},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            candles = await resp.json()
        # candles[-1] ist die noch laufende Kerze — nur candles[:-1] speichern
        for c in candles[:-1]:
            d = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).date()
            async with conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO ohlcv_daily (date, open, high, low, close, volume_icp, volume_usdt)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (date) DO NOTHING""",
                    (d, float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                     float(c[5]), float(c[7])),
                )
        await conn.commit()
        log.debug("ohlcv: stored candle(s)")
    except Exception as e:
        log.warning("ohlcv fetch error: %s", e)


async def backfill_ohlcv(conn, days: int = 500) -> None:
    """One-time backfill: fetch up to `days` historical daily candles if table is empty."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM ohlcv_daily")
        count = (await cur.fetchone())[0]
    if count > 0:
        log.info("ohlcv backfill: %d rows already present, skip", count)
        return

    log.info("ohlcv backfill: table empty, fetching %d days of history", days)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                KLINES_URL,
                params={"symbol": SYMBOL, "interval": "1d", "limit": days},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                candles = await resp.json()
        # Letzte Kerze ist noch offen — nicht speichern
        inserted = 0
        async with conn.cursor() as cur:
            for c in candles[:-1]:
                d = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).date()
                await cur.execute(
                    """INSERT INTO ohlcv_daily (date, open, high, low, close, volume_icp, volume_usdt)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (date) DO NOTHING""",
                    (d, float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                     float(c[5]), float(c[7])),
                )
                inserted += 1
        await conn.commit()
        log.info("ohlcv backfill: %d candles stored", inserted)
    except Exception as e:
        log.error("ohlcv backfill error: %s", e)


async def run_market_data() -> None:
    """Funding Rate + Open Interest — stündlich."""
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        async with aiohttp.ClientSession() as session:
            await collect_funding(session, conn)
            await collect_oi(session, conn)


async def run_ohlcv() -> None:
    """Tägliche OHLCV-Kerze speichern."""
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        async with aiohttp.ClientSession() as session:
            await collect_ohlcv(session, conn)


async def run_ohlcv_backfill() -> None:
    """Einmaliger historischer Backfill beim Start."""
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        await backfill_ohlcv(conn, days=500)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

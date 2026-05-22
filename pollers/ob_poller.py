"""
ob_poller.py — Binance Order Book Poller
Snapshots ICP/USDT bid/ask depth ±2% every 60 seconds.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
import psycopg

log = logging.getLogger(__name__)

SPOT_URL = "https://api.binance.com/api/v3/depth"
SYMBOL = "ICPUSDT"
DEPTH_LEVELS = 500    # fetch top 500 levels, filter ±2% ourselves
WINDOW_PCT = 0.02     # ±2% from mid price

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)


def _calc_depth(bids: list, asks: list) -> dict:
    """Calculate depth within ±2% of mid price."""
    if not bids or not asks:
        return {}

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2
    spread_bps = round((best_ask - best_bid) / mid * 10000, 2)

    lower = mid * (1 - WINDOW_PCT)
    upper = mid * (1 + WINDOW_PCT)

    bid_depth = sum(float(q) for p, q in bids if float(p) >= lower)
    ask_depth = sum(float(q) for p, q in asks if float(p) <= upper)

    return {
        "bid_depth_icp": round(bid_depth, 2),
        "ask_depth_icp": round(ask_depth, 2),
        "spread_bps": spread_bps,
        "mid_price_usdt": round(mid, 6),
    }


async def snapshot(session: aiohttp.ClientSession, conn) -> None:
    try:
        async with session.get(
            SPOT_URL,
            params={"symbol": SYMBOL, "limit": DEPTH_LEVELS},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    except Exception as e:
        log.warning("ob_poller fetch error: %s", e)
        return

    depth = _calc_depth(data.get("bids", []), data.get("asks", []))
    if not depth:
        return

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ob_snapshots (ts, bid_depth_icp, ask_depth_icp, spread_bps, mid_price_usdt)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                datetime.now(tz=timezone.utc),
                depth["bid_depth_icp"],
                depth["ask_depth_icp"],
                depth["spread_bps"],
                depth["mid_price_usdt"],
            ),
        )
    await conn.commit()
    log.debug(
        "ob_poller: mid=%.4f bid=%.0f ask=%.0f spread=%.1fbps",
        depth["mid_price_usdt"], depth["bid_depth_icp"], depth["ask_depth_icp"], depth["spread_bps"],
    )


async def run() -> None:
    """Single snapshot — called every 60s by runner."""
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        async with aiohttp.ClientSession() as session:
            await snapshot(session, conn)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

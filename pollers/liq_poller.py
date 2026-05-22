"""
liq_poller.py — Binance Futures Liquidation & Positioning Poller
Collects every 15 minutes:
  - Global Long/Short Account Ratio
  - Top Trader Long/Short Ratio
  - Taker Buy/Sell Ratio
  - Open Interest
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
import psycopg

log = logging.getLogger(__name__)

SYMBOL = "ICPUSDT"
BASE_FUTURES = "https://fapi.binance.com"
BASE_DATA = "https://fapi.binance.com/futures/data"

SKEW_ALERT_GLOBAL = 1.50   # global_ls_ratio über diesem Wert → Alert
SKEW_ALERT_TOP = 1.75      # top_ls_ratio über diesem Wert → Alert

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)


async def _get(session: aiohttp.ClientSession, url: str, params: dict) -> dict | list | None:
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()
    except Exception as e:
        log.warning("liq_poller fetch error %s: %s", url, e)
        return None


async def run() -> None:
    log.info("liq_poller: start")
    async with aiohttp.ClientSession() as session:
        # Alle 4 Endpoints parallel fetchen
        global_data, top_data, taker_data, oi_data = await asyncio.gather(
            _get(session, f"{BASE_DATA}/globalLongShortAccountRatio",
                 {"symbol": SYMBOL, "period": "15m", "limit": 1}),
            _get(session, f"{BASE_DATA}/topLongShortAccountRatio",
                 {"symbol": SYMBOL, "period": "15m", "limit": 1}),
            _get(session, f"{BASE_DATA}/takerlongshortRatio",
                 {"symbol": SYMBOL, "period": "15m", "limit": 1}),
            _get(session, f"{BASE_FUTURES}/fapi/v1/openInterest",
                 {"symbol": SYMBOL}),
        )

    if not all([global_data, top_data, taker_data, oi_data]):
        log.warning("liq_poller: incomplete data, skipping")
        return

    g = global_data[0] if isinstance(global_data, list) else global_data
    t = top_data[0] if isinstance(top_data, list) else top_data
    tk = taker_data[0] if isinstance(taker_data, list) else taker_data

    global_ls = float(g["longShortRatio"])
    top_ls = float(t["longShortRatio"])
    taker_ratio = float(tk["buySellRatio"])
    oi_icp = float(oi_data["openInterest"])

    skew = global_ls > SKEW_ALERT_GLOBAL or top_ls > SKEW_ALERT_TOP

    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO liquidation_snapshots (
                    ts,
                    global_long_pct, global_short_pct, global_ls_ratio,
                    top_long_pct,    top_short_pct,    top_ls_ratio,
                    taker_buy_sell_ratio, taker_buy_vol_icp, taker_sell_vol_icp,
                    open_interest_icp,
                    skew_alert
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    datetime.now(tz=timezone.utc),
                    float(g["longAccount"]),  float(g["shortAccount"]),  global_ls,
                    float(t["longAccount"]),  float(t["shortAccount"]),  top_ls,
                    taker_ratio,
                    float(tk["buyVol"]),
                    float(tk["sellVol"]),
                    oi_icp,
                    skew,
                ),
            )
        await conn.commit()

    if skew:
        log.info(
            "LIQ SKEW ALERT: global_ls=%.3f top_ls=%.3f taker=%.3f OI=%.0f ICP",
            global_ls, top_ls, taker_ratio, oi_icp,
        )
    else:
        log.debug(
            "liq_poller: global_ls=%.3f top_ls=%.3f taker=%.3f OI=%.0f ICP",
            global_ls, top_ls, taker_ratio, oi_icp,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

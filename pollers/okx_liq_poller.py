"""
okx_liq_poller.py — OKX Liquidation Events Poller
Sammelt ICP-USDT Perpetual Force Orders (Long & Short Liquidationen).
Polling alle 5 Minuten, Dedup via UNIQUE (ts, side, quantity_icp).
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
import psycopg

log = logging.getLogger(__name__)

OKX_URL = "https://www.okx.com/api/v5/public/liquidation-orders"
OKX_PARAMS = {
    "instType":   "SWAP",
    "instFamily": "ICP-USDT",
    "state":      "filled",
    "limit":      "100",
}

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)


async def run() -> None:
    log.info("okx_liq_poller: start")

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                OKX_URL, params=OKX_PARAMS,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                body = await resp.json()
        except Exception as e:
            log.warning("okx_liq_poller: fetch error: %s", e)
            return

    if body.get("code") != "0":
        log.warning("okx_liq_poller: API error %s — %s", body.get("code"), body.get("msg"))
        return

    events = []
    for bucket in body.get("data", []):
        for det in bucket.get("details", []):
            try:
                ts = datetime.fromtimestamp(int(det["ts"]) / 1000, tz=timezone.utc)
                side = det["posSide"]          # 'long' oder 'short'
                qty  = float(det["sz"])
                price = float(det["bkPx"])
                events.append((ts, side, qty, price))
            except (KeyError, ValueError) as e:
                log.debug("okx_liq_poller: skip event %s — %s", det, e)

    if not events:
        log.debug("okx_liq_poller: keine neuen Events")
        return

    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT MAX(ts) FROM liquidation_events"
            )
            row = await cur.fetchone()
            last_ts = row[0] if row and row[0] else datetime.fromtimestamp(0, tz=timezone.utc)

            new_events = [(ts, s, q, p) for ts, s, q, p in events if ts > last_ts]

            if new_events:
                await cur.executemany(
                    """
                    INSERT INTO liquidation_events (ts, side, quantity_icp, price)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (ts, side, quantity_icp) DO NOTHING
                    """,
                    new_events,
                )
                await conn.commit()
                longs  = sum(q for _, s, q, _ in new_events if s == "long")
                shorts = sum(q for _, s, q, _ in new_events if s == "short")
                log.info(
                    "okx_liq_poller: %d Events — Long %.0f ICP / Short %.0f ICP",
                    len(new_events), longs, shorts,
                )
            else:
                log.debug("okx_liq_poller: alle Events bereits bekannt")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

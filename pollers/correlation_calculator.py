"""
correlation_calculator.py — Pearson Korrelation Sell-Pressure / OI → Preis
Läuft alle 15 Minuten, schreibt in summary_stats.
"""
import asyncio
import logging
import math
import os

import psycopg

log = logging.getLogger(__name__)

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)

_CREATE = """
CREATE TABLE IF NOT EXISTS summary_stats (
    id              SERIAL PRIMARY KEY,
    sell_price_corr DOUBLE PRECISION,
    oi_price_corr   DOUBLE PRECISION,
    best_lag_hours  INT,
    calculated_at   TIMESTAMPTZ DEFAULT NOW()
);
"""

_FETCH = """
SELECT
    date_trunc('hour', o.ts)                                                        AS hour,
    AVG(l.taker_sell_vol_icp / NULLIF(l.taker_buy_vol_icp + l.taker_sell_vol_icp, 0)) AS sell_ratio,
    AVG(l.open_interest_icp)                                                        AS oi,
    AVG(o.mid_price_usdt)                                                           AS price
FROM ob_snapshots o
LEFT JOIN liquidation_snapshots l
       ON date_trunc('hour', l.ts) = date_trunc('hour', o.ts)
WHERE o.ts >= NOW() - INTERVAL '30 days'
GROUP BY 1
ORDER BY 1
"""


def _pearson(xs: list, ys: list) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


async def run() -> None:
    try:
        async with await psycopg.AsyncConnection.connect(_DSN) as conn:
            await conn.execute(_CREATE)

            rows = await (await conn.execute(_FETCH)).fetchall()
            n = len(rows)

            if n < 4:
                log.warning("correlation_calculator: nur %d Stunden Daten — überspringe", n)
                return

            sell_ratios = [float(r[1]) if r[1] is not None else None for r in rows]
            ois         = [float(r[2]) if r[2] is not None else None for r in rows]
            prices      = [float(r[3]) if r[3] is not None else None for r in rows]

            max_lag = min(24, n - 2)
            best_sell_r  = 0.0
            best_oi_r    = 0.0
            best_lag     = 1

            for lag in range(1, max_lag + 1):
                pairs_sell = [
                    (sell_ratios[i], prices[i + lag])
                    for i in range(n - lag)
                    if sell_ratios[i] is not None and prices[i + lag] is not None
                ]
                pairs_oi = [
                    (ois[i], prices[i + lag])
                    for i in range(n - lag)
                    if ois[i] is not None and prices[i + lag] is not None
                ]

                if pairs_sell:
                    xs, ys = zip(*pairs_sell)
                    r = _pearson(list(xs), list(ys))
                    if r is not None and abs(r) > abs(best_sell_r):
                        best_sell_r = r
                        best_lag    = lag

                if pairs_oi:
                    xs, ys = zip(*pairs_oi)
                    r = _pearson(list(xs), list(ys))
                    if r is not None and abs(r) > abs(best_oi_r):
                        best_oi_r = r

            await conn.execute(
                """
                INSERT INTO summary_stats (sell_price_corr, oi_price_corr, best_lag_hours, calculated_at)
                VALUES (%s, %s, %s, NOW())
                """,
                (best_sell_r, best_oi_r, best_lag),
            )
            log.info(
                "correlation: sell_r=%.3f  oi_r=%.3f  best_lag=%dh  (%d Stunden Daten)",
                best_sell_r, best_oi_r, best_lag, n,
            )

    except Exception:
        log.exception("correlation_calculator: Fehler")

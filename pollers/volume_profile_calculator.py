"""
volume_profile_calculator.py — Volume Profile (POC / VAH / VAL)
Berechnet täglich aus spot_trades (Tick-Daten) den echten Volume Profile.

Algorithmus:
  1. Alle Trades im Lookback-Fenster in Preisbuckets (BUCKET_SIZE USDT) einteilen
  2. Volumen pro Bucket summieren
  3. POC  = Bucket mit höchstem Volumen
  4. Value Area (70% des Gesamtvolumens):
     - Start am POC-Bucket
     - Schrittweise nach oben UND unten expandieren
     - Jeweils die Seite mit höherem nächsten Bucket-Volumen wählen
     - Stopp wenn 70% des Gesamtvolumens erreicht
  5. VAH = höchster Preis im Value-Area-Bereich
  6. VAL = niedrigster Preis im Value-Area-Bereich

Schedule: täglich 00:30 UTC (nach OHLCV-Fetch um 00:05)
"""
import asyncio
import logging
import os
from decimal import Decimal

import psycopg

log = logging.getLogger(__name__)

LOOKBACK_DAYS  = 30
BUCKET_SIZE    = Decimal("0.01")   # USDT pro Bucket (~0.4% bei $2.50)
VALUE_AREA_PCT = 0.70              # 70% Value Area

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)

_FETCH = """
SELECT price, SUM(quantity_icp) AS volume
FROM spot_trades
WHERE ts >= NOW() - INTERVAL '%s days'
GROUP BY price
ORDER BY price
"""

_INSERT = """
INSERT INTO volume_profile (calculated_at, lookback_days, poc_price, vah_price, val_price, total_volume_icp)
VALUES (NOW(), %s, %s, %s, %s, %s)
"""


def _bucket(price: Decimal) -> Decimal:
    return (price // BUCKET_SIZE) * BUCKET_SIZE


def _calc_profile(rows: list[tuple]) -> dict | None:
    if not rows:
        log.warning("volume_profile_calculator: keine Trades im Lookback-Fenster")
        return None

    # Volumen pro Bucket aggregieren
    buckets: dict[Decimal, Decimal] = {}
    for raw_price, raw_vol in rows:
        b = _bucket(Decimal(str(raw_price)))
        buckets[b] = buckets.get(b, Decimal(0)) + Decimal(str(raw_vol))

    if not buckets:
        return None

    total_volume = sum(buckets.values())
    target = total_volume * Decimal(str(VALUE_AREA_PCT))

    # POC = Bucket mit höchstem Volumen
    poc_bucket = max(buckets, key=lambda b: buckets[b])
    poc_price  = poc_bucket + BUCKET_SIZE / 2   # Bucket-Mittelpunkt

    # Value Area: vom POC aus nach oben/unten expandieren
    sorted_buckets = sorted(buckets.keys())
    poc_idx = sorted_buckets.index(poc_bucket)

    accumulated = buckets[poc_bucket]
    lo_idx = poc_idx
    hi_idx = poc_idx

    while accumulated < target:
        can_go_up   = hi_idx + 1 < len(sorted_buckets)
        can_go_down = lo_idx - 1 >= 0

        if not can_go_up and not can_go_down:
            break

        vol_up   = buckets[sorted_buckets[hi_idx + 1]] if can_go_up   else Decimal(0)
        vol_down = buckets[sorted_buckets[lo_idx - 1]] if can_go_down else Decimal(0)

        if vol_up >= vol_down:
            hi_idx += 1
            accumulated += vol_up
        else:
            lo_idx -= 1
            accumulated += vol_down

    vah_price = sorted_buckets[hi_idx] + BUCKET_SIZE   # Oberkante des höchsten Buckets
    val_price = sorted_buckets[lo_idx]                  # Unterkante des niedrigsten Buckets

    return {
        "poc_price":       float(poc_price),
        "vah_price":       float(vah_price),
        "val_price":       float(val_price),
        "total_volume":    float(total_volume),
        "bucket_count":    len(buckets),
        "va_coverage_pct": float(accumulated / total_volume * 100),
    }


async def run() -> None:
    log.info("volume_profile_calculator: start (lookback=%dd, bucket=%.3f USDT)",
             LOOKBACK_DAYS, BUCKET_SIZE)
    try:
        async with await psycopg.AsyncConnection.connect(_DSN) as conn:
            rows = await (await conn.execute(_FETCH, (LOOKBACK_DAYS,))).fetchall()
            log.info("volume_profile_calculator: %d Preisstufen geladen", len(rows))

            result = _calc_profile(rows)
            if result is None:
                return

            await conn.execute(
                _INSERT,
                (
                    LOOKBACK_DAYS,
                    result["poc_price"],
                    result["vah_price"],
                    result["val_price"],
                    result["total_volume"],
                ),
            )
            await conn.commit()

            log.info(
                "volume_profile_calculator: POC=%.4f  VAH=%.4f  VAL=%.4f  "
                "Vol=%.0f ICP  Buckets=%d  VA=%.1f%%",
                result["poc_price"], result["vah_price"], result["val_price"],
                result["total_volume"], result["bucket_count"], result["va_coverage_pct"],
            )

    except Exception:
        log.exception("volume_profile_calculator: Fehler")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

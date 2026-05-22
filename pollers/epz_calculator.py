"""
epz_calculator.py — Extreme Pressure Zone Composite Score
Läuft alle 15 Minuten nach liq_poller.
Liest aus liquidation_snapshots + ob_snapshots, schreibt in epz_scores.

5 Signale (alle aus DB, kein API-Call):
  1. Taker Sell Ratio      — aktueller aggressiver Sell-Druck          Gewicht: 32%
  2. Sell Momentum         — Beschleunigung des Sell-Drucks             Gewicht: 23%
  3. Price Drop            — Kursrückgang bestätigt den Druck           Gewicht: 20%
  4. OI Change             — steigendes OI bei Sells = Commitment       Gewicht: 15%
  5. L/S Shift             — mehr Shorts bei Top-Tradern                Gewicht: 10%

Schwellenwert: is_extreme = True wenn extreme_score >= 76
"""
import asyncio
import logging
import os

import psycopg

log = logging.getLogger(__name__)

EXTREME_THRESHOLD = 76.0

WEIGHTS = {
    "taker_sell":   0.32,
    "momentum":     0.23,
    "price_drop":   0.20,
    "oi_change":    0.15,
    "ls_shift":     0.10,
}

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)

_CREATE = """
CREATE TABLE IF NOT EXISTS epz_scores (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    price           DOUBLE PRECISION,
    sell_ratio      DOUBLE PRECISION,
    sell_momentum   DOUBLE PRECISION,
    price_drop_pct  DOUBLE PRECISION,
    oi_change_pct   DOUBLE PRECISION,
    ls_ratio        DOUBLE PRECISION,
    s_taker         DOUBLE PRECISION,
    s_momentum      DOUBLE PRECISION,
    s_delta         DOUBLE PRECISION,
    s_oi            DOUBLE PRECISION,
    s_ls            DOUBLE PRECISION,
    extreme_score   DOUBLE PRECISION NOT NULL,
    is_extreme      BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS epz_scores_ts_idx       ON epz_scores (ts DESC);
CREATE INDEX IF NOT EXISTS epz_scores_extreme_idx  ON epz_scores (is_extreme, ts DESC);
"""

# Holt alle Signal-Rohdaten in einer Abfrage.
# liq: letzte 6h aus liquidation_snapshots, mit ROW_NUMBER (1 = neuster Wert).
# signals: extrahiert current / recent_mean / older_mean / oi_3ago / ls_ratio.
# prices: mid_price jetzt vs. vor ~15 Minuten aus ob_snapshots.
_FETCH = """
WITH liq AS (
    SELECT
        CAST(taker_sell_vol_icp AS float8)
            / NULLIF(CAST(taker_buy_vol_icp + taker_sell_vol_icp AS float8), 0) AS sell_ratio,
        CAST(open_interest_icp AS float8)                                        AS oi,
        CAST(top_ls_ratio      AS float8)                                        AS ls_ratio,
        ROW_NUMBER() OVER (ORDER BY ts DESC)                                     AS rn
    FROM liquidation_snapshots
    WHERE ts >= NOW() - INTERVAL '6 hours'
),
signals AS (
    SELECT
        (SELECT sell_ratio FROM liq WHERE rn = 1)               AS current_sell,
        (SELECT AVG(sell_ratio) FROM liq WHERE rn <= 5)         AS recent_sell_mean,
        (SELECT AVG(sell_ratio) FROM liq WHERE rn BETWEEN 6 AND 15) AS older_sell_mean,
        (SELECT oi             FROM liq WHERE rn = 1)           AS oi_now,
        (SELECT oi             FROM liq WHERE rn = 4)           AS oi_3ago,
        (SELECT ls_ratio       FROM liq WHERE rn = 1)           AS ls_ratio
),
prices AS (
    SELECT
        (SELECT CAST(mid_price_usdt AS float8)
         FROM ob_snapshots ORDER BY ts DESC LIMIT 1)            AS price_now,
        (SELECT CAST(mid_price_usdt AS float8)
         FROM ob_snapshots
         WHERE ts <= NOW() - INTERVAL '14 minutes'
         ORDER BY ts DESC LIMIT 1)                              AS price_ago
)
SELECT
    s.current_sell,
    s.recent_sell_mean,
    s.older_sell_mean,
    s.oi_now,
    s.oi_3ago,
    s.ls_ratio,
    p.price_now,
    p.price_ago
FROM signals s, prices p
"""

_INSERT = """
INSERT INTO epz_scores (
    ts, price,
    sell_ratio, sell_momentum, price_drop_pct, oi_change_pct, ls_ratio,
    s_taker, s_momentum, s_delta, s_oi, s_ls,
    extreme_score, is_extreme
) VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _clip(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _score(row: tuple) -> dict:
    (
        current_sell, recent_sell_mean, older_sell_mean,
        oi_now, oi_3ago, ls_ratio,
        price_now, price_ago,
    ) = row

    # Rohdaten absichern
    current_sell     = float(current_sell)     if current_sell     is not None else 0.5
    recent_sell_mean = float(recent_sell_mean) if recent_sell_mean is not None else current_sell
    older_sell_mean  = float(older_sell_mean)  if older_sell_mean  is not None else current_sell
    oi_now           = float(oi_now)           if oi_now           is not None else 0.0
    oi_3ago          = float(oi_3ago)          if oi_3ago          is not None else oi_now
    ls_ratio         = float(ls_ratio)         if ls_ratio         is not None else 1.0
    price_now        = float(price_now)        if price_now        is not None else 0.0
    price_ago        = float(price_ago)        if price_ago        is not None else price_now

    # Abgeleitete Größen
    sell_momentum  = recent_sell_mean - older_sell_mean
    oi_change_pct  = (oi_now - oi_3ago) / oi_3ago * 100 if oi_3ago > 0 else 0.0
    price_drop_pct = (price_now - price_ago) / price_ago * 100 if price_ago > 0 else 0.0

    # Sub-Scores 0–100
    #   Kalibrierung: score=100 bei einem realistischen Extremwert
    #   taker_sell:  0 bei 50% Sell-Anteil, 100 bei 100%
    #   momentum:    100 bei +0.20 Shift (sehr starke Beschleunigung)
    #   price_drop:  100 bei -5% Rückgang in 15 Minuten
    #   oi_change:   100 bei +4% OI-Anstieg in 45 Minuten
    #   ls_shift:    100 bei ls_ratio ≤ 0.0 (Shorts dominieren komplett),
    #                0   bei ls_ratio ≥ 1.4 (Longs dominieren)
    s_taker    = _clip((current_sell - 0.50) * 200,          0.0, 100.0)
    s_momentum = _clip(sell_momentum         * 500,          0.0, 100.0)
    s_delta    = _clip(-price_drop_pct       * 20,           0.0, 100.0)
    s_oi       = _clip(oi_change_pct         * 25,           0.0, 100.0)
    s_ls       = _clip((1.4 - ls_ratio)      * 70,           0.0, 100.0)

    score = (
        s_taker    * WEIGHTS["taker_sell"] +
        s_momentum * WEIGHTS["momentum"]   +
        s_delta    * WEIGHTS["price_drop"] +
        s_oi       * WEIGHTS["oi_change"]  +
        s_ls       * WEIGHTS["ls_shift"]
    )

    return {
        "price":          price_now,
        "sell_ratio":     round(current_sell, 4),
        "sell_momentum":  round(sell_momentum, 4),
        "price_drop_pct": round(price_drop_pct, 2),
        "oi_change_pct":  round(oi_change_pct, 2),
        "ls_ratio":       round(ls_ratio, 3),
        "s_taker":        round(s_taker, 1),
        "s_momentum":     round(s_momentum, 1),
        "s_delta":        round(s_delta, 1),
        "s_oi":           round(s_oi, 1),
        "s_ls":           round(s_ls, 1),
        "extreme_score":  round(score, 1),
        "is_extreme":     score >= EXTREME_THRESHOLD,
    }


async def run() -> None:
    try:
        async with await psycopg.AsyncConnection.connect(_DSN) as conn:
            await conn.execute(_CREATE)

            row = await (await conn.execute(_FETCH)).fetchone()
            if row is None or row[0] is None:
                log.warning("epz_calculator: keine Daten in liquidation_snapshots — überspringe")
                return

            d = _score(row)

            await conn.execute(
                _INSERT,
                (
                    d["price"],
                    d["sell_ratio"],   d["sell_momentum"], d["price_drop_pct"],
                    d["oi_change_pct"], d["ls_ratio"],
                    d["s_taker"], d["s_momentum"], d["s_delta"], d["s_oi"], d["s_ls"],
                    d["extreme_score"], d["is_extreme"],
                ),
            )

            level = "EPZ ALARM" if d["is_extreme"] else "normal"
            log.info(
                "epz: score=%.1f [%s]  taker=%.3f(%.0f)  mom=%+.3f(%.0f)"
                "  drop=%.2f%%(%.0f)  oi=%+.2f%%(%.0f)  ls=%.3f(%.0f)  price=%.4f",
                d["extreme_score"], level,
                d["sell_ratio"],   d["s_taker"],
                d["sell_momentum"],d["s_momentum"],
                d["price_drop_pct"], d["s_delta"],
                d["oi_change_pct"],  d["s_oi"],
                d["ls_ratio"],       d["s_ls"],
                d["price"],
            )

    except Exception:
        log.exception("epz_calculator: Fehler")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

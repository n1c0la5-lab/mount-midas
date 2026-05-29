"""
threshold_calculator.py — Täglicher NP-Pflichtverkauf
Läuft täglich 05:00 UTC (nach np_poller 04:00 + dre_metrics 04:30).

Modell:
  - Belohnung aus np_reward_mints (aktuelle Periode, via DRE)
  - Pflichtverkauf = OPEX_RATIO × Tagesbelohnung
    (Schätzung: 60% des Rewards = Betriebskosten, bis opex_xdr_est gefüllt ist)
  - Wenn für einen NP kein Reward in np_reward_mints: Fallback via node_count × avg_reward
"""
import asyncio
import logging
import os
from datetime import date

import aiohttp
import psycopg

log = logging.getLogger(__name__)

OPEX_RATIO   = 0.60    # 60% = geschätzte Betriebskosten (bis Einzeldaten vorliegen)
DAYS_PER_MONTH = 30.0
IMF_URL = "https://www.imf.org/external/np/fin/data/rms_five.aspx?tsvflag=Y"

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)


# ── Daten-Fetch ───────────────────────────────────────────────────────────────

async def _fetch_xdr_usd(session: aiohttp.ClientSession) -> float:
    """Parst den IMF-TSV: USD/XDR (zweite U.S.-Dollar-Zeile = Currency units per SDR)."""
    async with session.get(IMF_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        text = await resp.text(encoding="utf-8", errors="ignore")

    usd_lines = [l for l in text.splitlines() if l.startswith("U.S. dollar")]
    if len(usd_lines) < 2:
        raise ValueError(f"IMF TSV: U.S.-Dollar-Zeilen nicht gefunden ({len(usd_lines)})")
    # Zeile 2: Currency units per SDR = USD/XDR
    parts = usd_lines[1].split("\t")
    return float(parts[1].strip())


async def _fetch_icp_usd(conn) -> float:
    """Letzter Spot-Preis aus spot_trades."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT price FROM spot_trades ORDER BY ts DESC LIMIT 1")
        row = await cur.fetchone()
    if not row:
        raise ValueError("spot_trades ist leer — kein ICP-Preis verfügbar")
    return float(row[0])


async def _fetch_rewards(conn) -> dict[str, float]:
    """
    Aktuelle Monatsbelohnung pro NP-Principal aus np_reward_mints.
    Nimmt die Periode mit dem höchsten ts (≈ letzter DRE-Lauf).
    """
    async with conn.cursor() as cur:
        await cur.execute("""
            SELECT DISTINCT ON (provider_principal)
                provider_principal, amount_icp
            FROM np_reward_mints
            ORDER BY provider_principal, ts DESC
        """)
        rows = await cur.fetchall()
    return {r[0]: float(r[1]) for r in rows}


async def _fetch_np_meta(conn) -> dict[str, dict]:
    """node_count, geography, hw_generation pro Principal."""
    async with conn.cursor() as cur:
        await cur.execute("""
            SELECT principal, node_count,
                   COALESCE(geography,     'unknown') AS geography,
                   COALESCE(hw_generation, 'unknown') AS hw_generation
            FROM np_providers
            WHERE node_count > 0
        """)
        rows = await cur.fetchall()
    return {r[0]: {"node_count": r[1], "geography": r[2], "hw_generation": r[3]}
            for r in rows}


# ── Berechnung ────────────────────────────────────────────────────────────────

def _calc_np(
    monthly_reward_icp: float,
    icp_price_usd: float,
    xdr_usd_rate: float,
) -> dict:
    icp_xdr_rate      = icp_price_usd / xdr_usd_rate          # ICP-Preis in XDR
    reward_xdr        = monthly_reward_icp * icp_xdr_rate      # monatlicher Reward in XDR

    daily_reward_icp  = monthly_reward_icp / DAYS_PER_MONTH
    opex_usd_daily    = daily_reward_icp * OPEX_RATIO * icp_price_usd
    mandatory_icp     = daily_reward_icp * OPEX_RATIO
    discretionary_icp = daily_reward_icp - mandatory_icp
    sell_pressure     = OPEX_RATIO

    return {
        "reward_xdr":          round(reward_xdr, 2),
        "opex_usd_est":        round(opex_usd_daily, 2),
        "mandatory_sell_usd":  round(mandatory_icp * icp_price_usd, 2),
        "mandatory_sell_icp":  round(mandatory_icp, 4),
        "total_reward_icp":    round(daily_reward_icp, 4),
        "discretionary_icp":   round(discretionary_icp, 4),
        "sell_pressure_ratio": round(sell_pressure, 4),
    }


# ── DB-Schreiben ──────────────────────────────────────────────────────────────

async def _write_per_np(conn, rows: list[tuple]) -> None:
    async with conn.cursor() as cur:
        for r in rows:
            await cur.execute("""
                INSERT INTO np_threshold_daily (
                    ts, principal, reward_xdr, xdr_usd_rate, icp_price_usdt,
                    opex_usd_est, mandatory_sell_usd, mandatory_sell_icp,
                    total_reward_icp, discretionary_icp, sell_pressure_ratio,
                    node_count, geography, hw_generation
                ) VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, r)
    await conn.commit()


async def _write_aggregate(
    conn,
    xdr_usd_rate: float,
    icp_price_usd: float,
    np_count: int,
    total_reward_icp: float,
    total_mandatory_icp: float,
    total_discretionary_icp: float,
    avg_sell_pressure: float,
) -> None:
    opex_usd_total = total_mandatory_icp * icp_price_usd
    async with conn.cursor() as cur:
        await cur.execute("""
            INSERT INTO threshold_aggregate_daily (
                ts, xdr_usd_rate, icp_price_usdt, np_count,
                total_reward_icp, total_mandatory_sell_icp, total_discretionary_icp,
                avg_sell_pressure_ratio,
                mandatory_sell_at_2usd, mandatory_sell_at_3usd, mandatory_sell_at_5usd
            ) VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            round(xdr_usd_rate,   4),
            round(icp_price_usd,  4),
            np_count,
            round(total_reward_icp,       2),
            round(total_mandatory_icp,    2),
            round(total_discretionary_icp, 2),
            round(avg_sell_pressure,      4),
            round(opex_usd_total / 2.0,   2),
            round(opex_usd_total / 3.0,   2),
            round(opex_usd_total / 5.0,   2),
        ))
    await conn.commit()


async def _store_xdr_rate(conn, rate: float) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO xdr_rates (ts, rate, source) VALUES (NOW(), %s, 'imf')",
            (round(rate, 6),)
        )
    await conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

async def run() -> None:
    log.info("threshold_calculator: start")

    async with aiohttp.ClientSession() as session:
        xdr_usd = await _fetch_xdr_usd(session)
    log.info("threshold_calculator: XDR/USD=%.4f", xdr_usd)

    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        icp_usd  = await _fetch_icp_usd(conn)
        rewards  = await _fetch_rewards(conn)
        np_meta  = await _fetch_np_meta(conn)
        await _store_xdr_rate(conn, xdr_usd)

    log.info("threshold_calculator: ICP/USD=%.4f | %d NPs mit Reward-Daten", icp_usd, len(rewards))

    rows: list[tuple] = []
    total_reward = total_mandatory = total_discretionary = 0.0
    sell_pressures: list[float] = []

    for principal, monthly_icp in rewards.items():
        if monthly_icp <= 0:
            continue
        meta = np_meta.get(principal, {"node_count": 0, "geography": "unknown", "hw_generation": "unknown"})
        c = _calc_np(monthly_icp, icp_usd, xdr_usd)

        total_reward      += c["total_reward_icp"]
        total_mandatory   += c["mandatory_sell_icp"]
        total_discretionary += c["discretionary_icp"]
        sell_pressures.append(c["sell_pressure_ratio"])

        rows.append((
            principal,
            c["reward_xdr"],
            xdr_usd,
            icp_usd,
            c["opex_usd_est"],
            c["mandatory_sell_usd"],
            c["mandatory_sell_icp"],
            c["total_reward_icp"],
            c["discretionary_icp"],
            c["sell_pressure_ratio"],
            meta["node_count"],
            meta["geography"],
            meta["hw_generation"],
        ))

    if not rows:
        log.warning("threshold_calculator: keine Reward-Daten — abgebrochen")
        return

    avg_pressure = sum(sell_pressures) / len(sell_pressures)

    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        await _write_per_np(conn, rows)
        await _write_aggregate(
            conn, xdr_usd, icp_usd, len(rows),
            total_reward, total_mandatory, total_discretionary, avg_pressure,
        )

    log.info(
        "threshold_calculator: %d NPs | tägl. Pflicht=%.0f ICP (%.0f USD) | "
        "Discretionary=%.0f ICP | Ratio=%.0f%%",
        len(rows),
        total_mandatory,
        total_mandatory * icp_usd,
        total_discretionary,
        avg_pressure * 100,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

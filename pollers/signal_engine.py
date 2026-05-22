"""
signal_engine.py — Mount Midas Signal Engine
Evaluates all 7 triggers every 60s, writes to signal_log, sends Telegram alerts.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp
import psycopg

log = logging.getLogger(__name__)

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCORE_ALERT          = 2
SCORE_MAX_ALERT      = 5
POST_MINT_BEAR_PCT   = 70.0
POST_MINT_BULL_PCT   = 30.0
POST_MINT_ALERT_COOLDOWN_H = 6

# Regime-Schwellenwerte (kalibriert via Backtesting Jan 2024 / Jan 2026)
REGIME_KOMPRESSION_STARK  = 10.0   # Monatlicher Flow < 10%  → stärkstes Setup
REGIME_KOMPRESSION        = 20.0   # Monatlicher Flow < 20%  → Feder gespannt
REGIME_KOMPRESSION_SCHWACH = 35.0  # Monatlicher Flow < 35%  → schwache Kompression
REGIME_DISTRIBUTION       = 65.0   # Monatlicher Flow > 65%  → Verkauf läuft
REGIME_DISTRIBUTION_STARK = 85.0   # Monatlicher Flow > 85%  → starke Distribution
REGIME_TRIGGER_VOL        = 1_000_000  # Tages-ICP für stilles-Laden-Signal
REGIME_TRIGGER_FLOW       = 10.0       # Tages-Flow % für stilles-Laden-Signal

TRIGGER_LABELS = {
    "mint":       "① Minting Event",
    "wallet_hop": "② Wallet Hop 0",
    "aggregator": "③ Aggregator aktiv",
    "ob_thin":    "④ OB Bid dünn",
    "ls_skew":    "⑤ L/S Skew > 5:1",
    "perp_spot":  "⑥ Perp/Spot > 3×",
    "sell_ratio": "⑦ Sell-Ratio > 75%",
}

REGIME_LABELS = {
    "KOMPRESSION_STARK":   "⚡ KOMPRESSION STARK",
    "KOMPRESSION":         "🟢 KOMPRESSION",
    "KOMPRESSION_SCHWACH": "🟡 KOMPRESSION (schwach)",
    "TRIGGER_BULLISH":     "⚠️ TRIGGER BULLISH — stilles Laden",
    "NEUTRAL":             "⚪ NEUTRAL",
    "DISTRIBUTION":        "🟠 DISTRIBUTION",
    "DISTRIBUTION_STARK":  "🔴 DISTRIBUTION STARK",
    "DISTRIBUTION_TOP":    "🔴🔴 TOP — EXIT",
}


async def _eval_triggers(conn) -> tuple[dict, dict]:
    """Evaluate all 7 triggers. Returns (triggers_bool_dict, meta_dict)."""
    t = {}
    meta = {}

    async with conn.cursor() as cur:
        # ① Minting Event — NNS hop_depth=0 within 48h
        await cur.execute(
            "SELECT COUNT(*) FROM wallet_movements "
            "WHERE hop_depth=0 AND ts >= NOW()-INTERVAL '48h'"
        )
        t["mint"] = ((await cur.fetchone())[0] or 0) > 0

        # ② Wallet Hop 0 — NP transfer within 6h
        await cur.execute(
            "SELECT COUNT(*) FROM wallet_movements "
            "WHERE hop_depth=0 AND ts >= NOW()-INTERVAL '6h'"
        )
        t["wallet_hop"] = ((await cur.fetchone())[0] or 0) > 0

        # ③ Aggregator aktiv — bef91947 within 6h
        await cur.execute(
            "SELECT COUNT(*) FROM wallet_movements "
            "WHERE from_principal LIKE 'bef91947%' AND ts >= NOW()-INTERVAL '6h'"
        )
        t["aggregator"] = ((await cur.fetchone())[0] or 0) > 0

        # ④ OB dünn — bid depth < 50k ICP
        await cur.execute(
            "SELECT bid_depth_icp, ask_depth_icp, mid_price_usdt "
            "FROM ob_snapshots ORDER BY ts DESC LIMIT 1"
        )
        row = await cur.fetchone()
        bid_depth  = float(row[0]) if row else 99999
        icp_price  = float(row[2]) if row else None
        t["ob_thin"] = bid_depth < 50_000
        meta["bid_depth"]  = bid_depth
        meta["icp_price"]  = icp_price

        # ⑤ L/S Skew > 5:1
        await cur.execute(
            "SELECT global_ls_ratio FROM liquidation_snapshots ORDER BY ts DESC LIMIT 1"
        )
        row = await cur.fetchone()
        ls_ratio = float(row[0]) if row else 0.0
        t["ls_skew"] = ls_ratio > 5
        meta["ls_ratio"] = ls_ratio

        # ⑥ Perp/Spot > 3×
        await cur.execute(
            "SELECT perp_spot_ratio FROM market_activity ORDER BY ts DESC LIMIT 1"
        )
        row = await cur.fetchone()
        perp_spot = float(row[0]) if row else 0.0
        t["perp_spot"] = perp_spot > 3
        meta["perp_spot"] = perp_spot

        # ⑦ Sell-Ratio > 75% — live from liquidation_snapshots
        await cur.execute(
            "SELECT COALESCE(AVG(taker_sell_vol_icp / "
            "  NULLIF(taker_buy_vol_icp + taker_sell_vol_icp, 0)), 0) "
            "FROM liquidation_snapshots WHERE ts >= NOW()-INTERVAL '24h'"
        )
        row = await cur.fetchone()
        sell_ratio = float(row[0]) if row else 0.0
        t["sell_ratio"] = sell_ratio > 0.75
        meta["sell_ratio_pct"] = round(sell_ratio * 100, 1)

        # EPZ — latest composite score + sub-scores
        await cur.execute(
            "SELECT extreme_score, is_extreme, s_taker, s_momentum, s_delta, s_oi, s_ls "
            "FROM epz_scores ORDER BY ts DESC LIMIT 1"
        )
        row = await cur.fetchone()
        if row:
            meta["epz_score"]      = round(float(row[0]), 1)
            meta["epz_is_extreme"] = bool(row[1])
            meta["epz_s_taker"]    = int(float(row[2]))
            meta["epz_s_momentum"] = int(float(row[3]))
            meta["epz_s_delta"]    = int(float(row[4]))
            meta["epz_s_oi"]       = int(float(row[5]))
            meta["epz_s_ls"]       = int(float(row[6]))
        else:
            meta["epz_score"]      = None
            meta["epz_is_extreme"] = False

    return t, meta


async def _eval_post_mint_flow(conn) -> dict:
    """
    Berechnet Exchange-Flow % der letzten 30 Tage (monatliche Ebene).
    Zählt Hop-0 direkt zu Exchanges UND Hop-0 → Broker → Exchange (Hop-1).
    """
    async with conn.cursor() as cur:
        await cur.execute("""
            SELECT
                COALESCE(SUM(wm.amount_icp), 0) AS total_icp,
                COALESCE(SUM(wm.amount_icp) FILTER (
                    WHERE wl_d.label IN ('exchange', 'aggregator')
                       OR wl_h.label IN ('exchange', 'aggregator')
                ), 0) AS exchange_icp
            FROM wallet_movements wm
            LEFT JOIN np_wallet_labels wl_d ON wl_d.account_id = wm.to_principal
            LEFT JOIN wallet_movements wm_h
                ON wm_h.from_principal = wm.to_principal AND wm_h.hop_depth = 1
            LEFT JOIN np_wallet_labels wl_h ON wl_h.account_id = wm_h.to_principal
            WHERE wm.hop_depth = 0
              AND wm.ts >= NOW() - INTERVAL '30 days'
        """)
        row = await cur.fetchone()
    total_icp    = float(row[0]) if row else 0.0
    exchange_icp = float(row[1]) if row else 0.0
    flow_pct = (exchange_icp / total_icp * 100) if total_icp > 0 else None
    return {
        "flow_pct":     round(flow_pct, 1) if flow_pct is not None else None,
        "total_icp":    round(total_icp, 0),
        "exchange_icp": round(exchange_icp, 0),
    }


async def _eval_daily_np_flow(conn) -> dict:
    """
    Tages-Flow: heutiges NP-Hop-0-Volumen und Exchange-Anteil.
    Schlüsselsignal für TRIGGER_BULLISH (großes Volumen + niedriger Flow).
    """
    async with conn.cursor() as cur:
        await cur.execute("""
            SELECT
                COALESCE(SUM(wm.amount_icp), 0) AS total_icp,
                COALESCE(SUM(wm.amount_icp) FILTER (
                    WHERE wl_d.label IN ('exchange', 'aggregator')
                       OR wl_h.label IN ('exchange', 'aggregator')
                ), 0) AS exchange_icp
            FROM wallet_movements wm
            LEFT JOIN np_wallet_labels wl_d ON wl_d.account_id = wm.to_principal
            LEFT JOIN wallet_movements wm_h
                ON wm_h.from_principal = wm.to_principal AND wm_h.hop_depth = 1
            LEFT JOIN np_wallet_labels wl_h ON wl_h.account_id = wm_h.to_principal
            WHERE wm.hop_depth = 0
              AND wm.ts >= CURRENT_DATE
        """)
        row = await cur.fetchone()
    total_icp    = float(row[0]) if row else 0.0
    exchange_icp = float(row[1]) if row else 0.0
    flow_pct = (exchange_icp / total_icp * 100) if total_icp > 0 else None
    return {
        "flow_pct":  round(flow_pct, 1) if flow_pct is not None else None,
        "total_icp": round(total_icp, 0),
    }


def detect_regime(t: dict, meta: dict, pm: dict, daily: dict) -> str:
    """
    Regime-Erkennung basierend auf Backtesting (Jan 2024 / Nov 2025 / Jan 2026).

    Priorität (von oben nach unten):
    1. Tages-TOP-Signal: hoher Tages-Flow → EXIT
    2. Tages-TRIGGER: großes Volumen + niedriger Tages-Flow → stilles Laden
    3. Monatlicher Flow für strukturelles Regime
    """
    monthly_flow  = pm.get("flow_pct") or 0.0
    daily_flow    = daily.get("flow_pct")
    daily_vol     = daily.get("total_icp") or 0.0
    epz           = meta.get("epz_score") or 0.0
    hop_active    = t.get("wallet_hop") or t.get("mint")

    # 1. Tages-TOP-Signal: Flow >85% an einem Tag mit signifikantem Volumen
    if daily_flow is not None and daily_flow > REGIME_DISTRIBUTION_STARK and daily_vol > 100_000:
        return "DISTRIBUTION_TOP"

    # 2. Tages-TRIGGER: großes Volumen + sehr niedriger Flow = stilles Laden
    if daily_vol >= REGIME_TRIGGER_VOL and daily_flow is not None and daily_flow < REGIME_TRIGGER_FLOW:
        return "TRIGGER_BULLISH"

    # 3. Monatliche Ebene
    if monthly_flow < REGIME_KOMPRESSION_STARK and not hop_active and epz < 30:
        return "KOMPRESSION_STARK"

    if monthly_flow < REGIME_KOMPRESSION and not hop_active and epz < 40:
        return "KOMPRESSION"

    if monthly_flow < REGIME_KOMPRESSION_SCHWACH:
        return "KOMPRESSION_SCHWACH"

    if monthly_flow > REGIME_DISTRIBUTION_STARK and hop_active:
        return "DISTRIBUTION_STARK"

    if monthly_flow > REGIME_DISTRIBUTION and hop_active:
        return "DISTRIBUTION"

    return "NEUTRAL"


async def _post_mint_alert_due(conn) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("""
            SELECT 1 FROM signal_log
            WHERE details->>'post_mint_alerted' = 'true'
              AND ts >= NOW() - INTERVAL '%s hours'
            LIMIT 1
        """, (POST_MINT_ALERT_COOLDOWN_H,))
        return await cur.fetchone() is None


async def _last_score(conn) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT score FROM signal_log ORDER BY ts DESC LIMIT 1")
        row = await cur.fetchone()
        return row[0] if row else -1


async def _write_log(
    conn, score: int, t: dict, meta: dict,
    regime: str, post_mint_alerted: bool = False,
) -> None:
    details = {
        "trigger_aggregator":  t.get("aggregator", False),
        "trigger_ls_skew":     t.get("ls_skew",    False),
        "trigger_perp_spot":   t.get("perp_spot",  False),
        "sell_ratio_pct":      meta.get("sell_ratio_pct"),
        "ls_ratio":            meta.get("ls_ratio"),
        "perp_spot":           meta.get("perp_spot"),
        "post_mint_flow_pct":  meta.get("post_mint_flow_pct"),
        "daily_flow_pct":      meta.get("daily_flow_pct"),
        "daily_icp_vol":       meta.get("daily_icp_vol"),
        "post_mint_alerted":   "true" if post_mint_alerted else "false",
    }
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO signal_log (
                ts, score,
                trigger_mint, trigger_wallet, trigger_ob_thin, trigger_threshold,
                icp_price_usdt, ob_depth_icp, details, alerted, regime
            ) VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s::jsonb, false, %s)
            """,
            (
                score,
                t.get("mint",       False),
                t.get("wallet_hop", False),
                t.get("ob_thin",    False),
                t.get("sell_ratio", False),
                meta.get("icp_price"),
                meta.get("bid_depth"),
                json.dumps(details, default=str),
                regime,
            ),
        )
    await conn.commit()


async def _send_telegram(session: aiohttp.ClientSession, text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("signal_engine: Telegram not configured — alert skipped")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with session.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                log.info("signal_engine: Telegram alert sent")
            else:
                log.warning("signal_engine: Telegram error %s", await resp.text())
    except Exception as e:
        log.warning("signal_engine: Telegram send failed: %s", e)


def _epz_block(meta: dict) -> str:
    epz = meta.get("epz_score")
    if epz is None:
        return ""
    if meta.get("epz_is_extreme"):
        return (
            f"\n\n🔴 <b>EPZ: {epz}/100 — EXTREME ZONE</b>\n"
            f"  Taker <b>{meta.get('epz_s_taker',0)}</b>"
            f"  Momentum <b>{meta.get('epz_s_momentum',0)}</b>"
            f"  Price Drop <b>{meta.get('epz_s_delta',0)}</b>"
            f"  OI <b>{meta.get('epz_s_oi',0)}</b>"
            f"  L/S <b>{meta.get('epz_s_ls',0)}</b>"
        )
    return f"\n\n🟢 EPZ: {epz}/100 — Normal"


def _build_alert(score: int, t: dict, meta: dict, regime: str) -> str:
    active_lines = "\n".join(
        f"  🔴 {TRIGGER_LABELS[k]}" for k, v in t.items() if v
    )
    price  = meta.get("icp_price", "?")
    sell   = meta.get("sell_ratio_pct", "?")
    rlabel = REGIME_LABELS.get(regime, regime)
    header = (
        f"🚨 <b>MOUNT MIDAS MAX ALERT</b> — Score {score}/7"
        if score >= SCORE_MAX_ALERT
        else f"⚠️ <b>MOUNT MIDAS ALERT</b> — Score {score}/7"
    )
    return (
        f"{header}\n"
        f"Regime: <b>{rlabel}</b>\n"
        f"ICP/USDT: <b>${price}</b>  |  Sell-Ratio: <b>{sell}%</b>"
        f"{_epz_block(meta)}\n\n"
        f"Aktive Trigger:\n{active_lines}"
    )


async def run() -> None:
    log.info("signal_engine: start")
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        async with aiohttp.ClientSession() as session:
            t, meta = await _eval_triggers(conn)
            score = sum(1 for v in t.values() if v)
            last  = await _last_score(conn)

            pm    = await _eval_post_mint_flow(conn)
            daily = await _eval_daily_np_flow(conn)

            meta["post_mint_flow_pct"]  = pm["flow_pct"]
            meta["post_mint_total_icp"] = pm["total_icp"]
            meta["post_mint_exch_icp"]  = pm["exchange_icp"]
            meta["daily_flow_pct"]      = daily["flow_pct"]
            meta["daily_icp_vol"]       = daily["total_icp"]

            regime = detect_regime(t, meta, pm, daily)

            log.info(
                "signal_engine: score=%d/7  regime=%s  monthly_flow=%s%%  daily_flow=%s%%  daily_vol=%.0f ICP",
                score, regime,
                pm["flow_pct"] if pm["flow_pct"] is not None else "n/a",
                daily["flow_pct"] if daily["flow_pct"] is not None else "n/a",
                daily["total_icp"],
            )

            post_mint_alerted = False
            flow = pm["flow_pct"]
            if flow is not None and await _post_mint_alert_due(conn):
                if flow > POST_MINT_BEAR_PCT:
                    msg = (
                        f"⚠️ <b>Post-Mint Flow &gt;{POST_MINT_BEAR_PCT:.0f}% zu Exchanges</b>\n"
                        f"Exchange-Flow (30 Tage): <b>{flow}%</b> — Bearish\n"
                        f"Gesamt: {pm['total_icp']:,.0f} ICP  |  Exchanges: {pm['exchange_icp']:,.0f} ICP"
                    )
                    await _send_telegram(session, msg)
                    post_mint_alerted = True
                elif flow < POST_MINT_BULL_PCT and pm["total_icp"] > 1000:
                    msg = (
                        f"🟢 <b>Post-Mint Flow &lt;{POST_MINT_BULL_PCT:.0f}% zu Exchanges</b>\n"
                        f"Exchange-Flow (30 Tage): <b>{flow}%</b> — Bullish\n"
                        f"Gesamt: {pm['total_icp']:,.0f} ICP  |  Exchanges: {pm['exchange_icp']:,.0f} ICP"
                    )
                    await _send_telegram(session, msg)
                    post_mint_alerted = True

            # Regime-Alert bei Wechsel zu KOMPRESSION oder TRIGGER
            if regime in ("KOMPRESSION_STARK", "KOMPRESSION", "TRIGGER_BULLISH"):
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT regime FROM signal_log ORDER BY ts DESC LIMIT 1"
                    )
                    row = await cur.fetchone()
                    last_regime = row[0] if row else None
                if last_regime != regime:
                    rlabel = REGIME_LABELS.get(regime, regime)
                    msg = (
                        f"{'⚡' if 'STARK' in regime else '🟢'} <b>Regime-Wechsel: {rlabel}</b>\n"
                        f"Monthly Flow: <b>{flow}%</b>  |  Daily Vol: <b>{daily['total_icp']:,.0f} ICP</b>\n"
                        f"Daily Flow: <b>{daily['flow_pct']}%</b>"
                    )
                    await _send_telegram(session, msg)

            await _write_log(conn, score, t, meta, regime, post_mint_alerted)

            if score >= SCORE_ALERT and score > last:
                await _send_telegram(session, _build_alert(score, t, meta, regime))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

"""
signal_engine.py — Mount Midas Signal Engine
Evaluates all triggers every 60s, writes to signal_log, sends Telegram alerts.
Nachtruhe 00:00–07:00 Europe/Bucharest — kein Alert in dieser Zeit.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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
OLLAMA_URL       = os.environ.get("OLLAMA_HOST", os.environ.get("OLLAMA_URL", ""))
OLLAMA_MODEL     = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")

BUCHAREST_TZ     = ZoneInfo("Europe/Bucharest")
QUIET_HOUR_START = 0   # 00:00 Uhr
QUIET_HOUR_END   = 7   # 07:00 Uhr
SCORE_ALERT          = 2
SCORE_MAX_ALERT      = 5
POST_MINT_BEAR_PCT   = 70.0
POST_MINT_BULL_PCT   = 30.0
POST_MINT_ALERT_COOLDOWN_H = 6
SELL_RATIO_WINDOW_H    = 2        # war 24h — reaktiver für Intraday-Signal
LARGE_SELL_MIN_ICP     = 6_000    # einzelner Taker-Sell-Schwellenwert
LARGE_SELL_COUNT_MIN   = 3        # Mindestanzahl Cluster-Sells in Zeitfenster
LARGE_SELL_WINDOW_MIN  = 30       # Lookback-Fenster für Cluster-Erkennung (war 10min)
ALERT_COOLDOWN_COMBINED_MIN = 30  # Cooldown wenn ⑨+⑩ gleichzeitig aktiv
FUNDING_SPIKE_RATE     = 0.0005   # > 0.05% = Retail überleveraged long
CVD_WINDOW_H           = 1        # netto CVD Berechnungsfenster
CVD_PRICE_WINDOW_H     = 4        # Preisreferenz-Fenster für Divergenz-Check
CVD_DIVERGE_MIN_PCT    = 1.0      # minimale Preissteigung für Divergenz-Check
CVD_NET_THRESHOLD      = -5_000   # negativer CVD-Schwellenwert (ICP)
ALERT_COOLDOWN_H       = 2        # Mindestabstand zwischen Score-Alerts

# Regime-Schwellenwerte (kalibriert via Backtesting Jan 2024 / Jan 2026)
REGIME_KOMPRESSION_STARK  = 10.0   # Monatlicher Flow < 10%  → stärkstes Setup
REGIME_KOMPRESSION        = 20.0   # Monatlicher Flow < 20%  → Feder gespannt
REGIME_KOMPRESSION_SCHWACH = 35.0  # Monatlicher Flow < 35%  → schwache Kompression
REGIME_DISTRIBUTION       = 65.0   # Monatlicher Flow > 65%  → Verkauf läuft
REGIME_DISTRIBUTION_STARK = 85.0   # Monatlicher Flow > 85%  → starke Distribution
REGIME_TRIGGER_VOL        = 1_000_000  # Tages-ICP für stilles-Laden-Signal
REGIME_TRIGGER_FLOW       = 10.0       # Tages-Flow % für stilles-Laden-Signal

TRIGGER_LABELS = {
    "mint":          "① Minting Event",
    "wallet_hop":    "② Wallet Hop 0",
    "aggregator":    "③ Aggregator aktiv",
    "ob_thin":       "④ OB Bid dünn",
    "ls_skew":       "⑤ L/S Skew > 5:1",
    "perp_spot":     "⑥ Perp/Spot > 3×",
    "sell_ratio":    "⑦ Sell-Ratio > 75% (2h)",
    "hop_spike":     "⑧ Hop-2/3-Spike",
    "cvd_div":       "⑨ CVD-Divergenz",
    "large_sell":    "⑩ Large Ask Sell",
    "funding_spike": "⑪ Funding Spike",
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
    """Evaluate all 8 triggers. Returns (triggers_bool_dict, meta_dict)."""
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

        # ⑦ Sell-Ratio > 75% — 2h Fenster (war 24h: zu träge für Intraday)
        await cur.execute(
            "SELECT COALESCE(AVG(taker_sell_vol_icp / "
            "  NULLIF(taker_buy_vol_icp + taker_sell_vol_icp, 0)), 0) "
            f"FROM liquidation_snapshots WHERE ts >= NOW()-INTERVAL '{SELL_RATIO_WINDOW_H}h'"
        )
        row = await cur.fetchone()
        sell_ratio = float(row[0]) if row else 0.0
        t["sell_ratio"] = sell_ratio > 0.75
        meta["sell_ratio_pct"] = round(sell_ratio * 100, 1)

        # ⑧ Hop-2/3-Spike — current 6h volume > 1.5× 3-day rolling avg per 6h window
        await cur.execute("""
            WITH cur AS (
                SELECT COALESCE(SUM(amount_icp), 0) AS vol
                FROM wallet_movements
                WHERE hop_depth >= 2 AND ts >= NOW() - INTERVAL '6h'
            ),
            base AS (
                SELECT COALESCE(SUM(amount_icp), 0) / 12.0 AS avg_6h
                FROM wallet_movements
                WHERE hop_depth >= 2
                  AND ts >= NOW() - INTERVAL '3 days'
                  AND ts < NOW() - INTERVAL '6h'
            )
            SELECT cur.vol, base.avg_6h
            FROM cur, base
        """)
        row = await cur.fetchone()
        hop_vol_6h   = float(row[0]) if row else 0.0
        hop_avg_6h   = float(row[1]) if row else 0.0
        t["hop_spike"] = hop_vol_6h > max(hop_avg_6h * 1.5, 50_000)
        meta["hop_vol_6h"]  = round(hop_vol_6h, 0)
        meta["hop_avg_6h"]  = round(hop_avg_6h, 0)

        # ⑨ CVD-Divergenz: Preis +X% im Referenzfenster, aber netto CVD negativ
        await cur.execute(f"""
            WITH price_bounds AS (
                SELECT
                    (SELECT price FROM spot_trades
                     WHERE ts >= NOW() - INTERVAL '{CVD_PRICE_WINDOW_H} hours'
                     ORDER BY ts ASC LIMIT 1) AS price_open,
                    (SELECT price FROM spot_trades ORDER BY ts DESC LIMIT 1) AS price_now
            ),
            cvd_recent AS (
                SELECT COALESCE(SUM(
                    CASE WHEN NOT is_buyer_maker THEN quantity_icp ELSE -quantity_icp END
                ), 0) AS net_vol
                FROM spot_trades
                WHERE ts >= NOW() - INTERVAL '{CVD_WINDOW_H} hours'
            )
            SELECT pb.price_open, pb.price_now, cr.net_vol
            FROM price_bounds pb, cvd_recent cr
        """)
        row = await cur.fetchone()
        if row and row[0] and row[1] and float(row[0]) > 0:
            price_change_pct = (float(row[1]) - float(row[0])) / float(row[0]) * 100
            cvd_net = float(row[2] or 0)
            t["cvd_div"] = price_change_pct >= CVD_DIVERGE_MIN_PCT and cvd_net < CVD_NET_THRESHOLD
            meta["cvd_price_chg_pct"] = round(price_change_pct, 2)
            meta["cvd_net_1h"] = round(cvd_net, 0)
        else:
            t["cvd_div"] = False
            meta["cvd_price_chg_pct"] = None
            meta["cvd_net_1h"] = None

        # ⑩ Large Sell Cluster: ≥N Taker-Sells > Schwellenwert in 30min UND CVD negativ
        # Filtert TWAP-Orders heraus — nur echte Cluster (Distribution/Bounce-Selling)
        await cur.execute(f"""
            SELECT COUNT(*) FROM spot_trades
            WHERE is_buyer_maker = true
              AND quantity_icp > {LARGE_SELL_MIN_ICP}
              AND ts >= NOW() - INTERVAL '{LARGE_SELL_WINDOW_MIN} minutes'
        """)
        sell_count = (await cur.fetchone())[0] or 0
        cvd_net_1h = meta.get("cvd_net_1h")
        t["large_sell"] = (
            sell_count >= LARGE_SELL_COUNT_MIN
            and cvd_net_1h is not None
            and cvd_net_1h < 0
        )
        meta["large_sell_count"] = sell_count

        # ⑪ Funding Rate Spike — Retail überleveraged long → Liquidierungskaskade vorbereitet
        await cur.execute(
            "SELECT funding_rate FROM funding_rates ORDER BY ts DESC LIMIT 1"
        )
        row = await cur.fetchone()
        funding_rate = float(row[0]) if row else 0.0
        t["funding_spike"] = funding_rate > FUNDING_SPIKE_RATE
        meta["funding_rate_pct"] = round(funding_rate * 100, 4)

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
    hop_active    = t.get("wallet_hop") or t.get("mint") or t.get("hop_spike")

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


async def _alert_cooldown_active(conn, cooldown_h: float = ALERT_COOLDOWN_H) -> bool:
    """True wenn in den letzten cooldown_h Stunden bereits ein Score-Alert gesendet wurde."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT 1 FROM signal_log WHERE alerted = true "
            f"AND ts >= NOW() - INTERVAL '{cooldown_h} hours' LIMIT 1"
        )
        return await cur.fetchone() is not None


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
    regime: str, post_mint_alerted: bool = False, alerted: bool = False,
) -> None:
    details = {
        "trigger_aggregator":    t.get("aggregator",    False),
        "trigger_ls_skew":       t.get("ls_skew",       False),
        "trigger_perp_spot":     t.get("perp_spot",     False),
        "trigger_hop_spike":     t.get("hop_spike",     False),
        "trigger_cvd_div":       t.get("cvd_div",       False),
        "trigger_large_sell":    t.get("large_sell",    False),
        "trigger_funding_spike": t.get("funding_spike", False),
        "sell_ratio_pct":        meta.get("sell_ratio_pct"),
        "ls_ratio":              meta.get("ls_ratio"),
        "perp_spot":             meta.get("perp_spot"),
        "post_mint_flow_pct":    meta.get("post_mint_flow_pct"),
        "daily_flow_pct":        meta.get("daily_flow_pct"),
        "daily_icp_vol":         meta.get("daily_icp_vol"),
        "post_mint_alerted":     "true" if post_mint_alerted else "false",
        "hop_vol_6h":            meta.get("hop_vol_6h"),
        "hop_avg_6h":            meta.get("hop_avg_6h"),
        "cvd_price_chg_pct":     meta.get("cvd_price_chg_pct"),
        "cvd_net_1h":            meta.get("cvd_net_1h"),
        "funding_rate_pct":      meta.get("funding_rate_pct"),
    }
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO signal_log (
                ts, score,
                trigger_mint, trigger_wallet, trigger_ob_thin, trigger_threshold,
                icp_price_usdt, ob_depth_icp, details, alerted, regime
            ) VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
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
                alerted,
                regime,
            ),
        )
    await conn.commit()


def _is_quiet_hours() -> bool:
    """Nachtruhe 00:00–07:00 Europe/Bucharest — kein Telegram in dieser Zeit."""
    hour = datetime.now(BUCHAREST_TZ).hour
    return QUIET_HOUR_START <= hour < QUIET_HOUR_END


async def _llm_comment(
    session: aiohttp.ClientSession,
    score: int,
    t: dict,
    meta: dict,
    regime: str,
) -> str | None:
    """Generiert 2–3 Satz Analyse via Ollama qwen2.5. None bei Fehler/Timeout."""
    if not OLLAMA_URL:
        return None

    active = ", ".join(TRIGGER_LABELS[k] for k, v in t.items() if v) or "keine"
    rlabel = REGIME_LABELS.get(regime, regime)
    price  = meta.get("icp_price", "?")
    sell   = meta.get("sell_ratio_pct", "?")
    cvd    = meta.get("cvd_net_1h")
    cvd_str = f"{cvd:+.0f} ICP" if cvd is not None else "n/a"
    funding = meta.get("funding_rate_pct")
    funding_str = f"{funding:+.4f}%" if funding is not None else "n/a"
    sell_count = meta.get("large_sell_count", 0)

    prompt = (
        f"Du bist ein Krypto-Marktanalyst spezialisiert auf Internet Computer (ICP/USDT), "
        f"eine Blockchain-Kryptowährung. Antworte in 2–3 präzisen Sätzen auf Deutsch. "
        f"Kein Markdown, kein Aufzählungszeichen, direkter Ton.\n\n"
        f"Aktueller Marktzustand:\n"
        f"Score: {score}/11 | Regime: {rlabel}\n"
        f"Aktive Trigger: {active}\n"
        f"ICP-Preis: ${price} | Sell-Ratio: {sell}% | Funding: {funding_str}\n"
        f"CVD (1h netto): {cvd_str} | Large Sells (30min): {sell_count}\n\n"
        f"Was bedeutet dieses Signal konkret? Worauf sollte man jetzt achten?"
    )

    try:
        async with session.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.3, "num_predict": 150}},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log.warning("llm_comment: Ollama HTTP %s", resp.status)
                return None
            data = await resp.json()
            text = (data.get("response") or "").strip()
            return text if text else None
    except Exception as e:
        log.warning("llm_comment: %s", e)
        return None


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


def _build_alert(score: int, t: dict, meta: dict, regime: str, llm_text: str | None = None) -> str:
    active_lines = "\n".join(
        f"  🔴 {TRIGGER_LABELS[k]}" for k, v in t.items() if v
    )
    price   = meta.get("icp_price", "?")
    sell    = meta.get("sell_ratio_pct", "?")
    funding = meta.get("funding_rate_pct")
    funding_str = f"  |  Funding: <b>{funding:+.4f}%</b>" if funding is not None else ""
    rlabel  = REGIME_LABELS.get(regime, regime)
    header  = (
        f"🚨 <b>MOUNT MIDAS MAX ALERT</b> — Score {score}/11"
        if score >= SCORE_MAX_ALERT
        else f"⚠️ <b>MOUNT MIDAS ALERT</b> — Score {score}/11"
    )
    llm_block = f"\n\n─────────────\n📊 <i>{llm_text}</i>" if llm_text else ""
    return (
        f"{header}\n"
        f"Regime: <b>{rlabel}</b>\n"
        f"ICP/USDT: <b>${price}</b>  |  Sell-Ratio: <b>{sell}%</b>{funding_str}"
        f"{_epz_block(meta)}\n\n"
        f"Aktive Trigger:\n{active_lines}"
        f"{llm_block}"
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
                "signal_engine: score=%d/11  regime=%s  monthly_flow=%s%%  daily_flow=%s%%  "
                "daily_vol=%.0f ICP  cvd_net=%.0f  funding=%.4f%%",
                score, regime,
                pm["flow_pct"] if pm["flow_pct"] is not None else "n/a",
                daily["flow_pct"] if daily["flow_pct"] is not None else "n/a",
                daily["total_icp"],
                meta.get("cvd_net_1h") or 0,
                meta.get("funding_rate_pct") or 0,
            )

            quiet = _is_quiet_hours()
            if quiet:
                log.debug("signal_engine: Nachtruhe aktiv — kein Telegram")

            post_mint_alerted = False
            flow = pm["flow_pct"]
            if not quiet and flow is not None and await _post_mint_alert_due(conn):
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
            if not quiet and regime in ("KOMPRESSION_STARK", "KOMPRESSION", "TRIGGER_BULLISH"):
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

            # Score-Alert: kürzer Cooldown wenn ⑨+⑩ gleichzeitig (präzises Distribution-Signal)
            combined_signal = t.get("cvd_div") and t.get("large_sell")
            cooldown_h = ALERT_COOLDOWN_COMBINED_MIN / 60 if combined_signal else ALERT_COOLDOWN_H
            should_alert = (
                score >= SCORE_ALERT
                and score > last
                and not await _alert_cooldown_active(conn, cooldown_h)
            )
            await _write_log(conn, score, t, meta, regime, post_mint_alerted, alerted=should_alert)

            if should_alert and not quiet:
                llm_text = await _llm_comment(session, score, t, meta, regime)
                await _send_telegram(session, _build_alert(score, t, meta, regime, llm_text))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

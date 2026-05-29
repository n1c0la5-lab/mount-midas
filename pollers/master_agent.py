"""
master_agent.py — Mount Midas Master Agent ("Midas Brain", MM-07)

Orchestrierungsschicht über den bestehenden Sub-Systemen. Liest die DB,
synthetisiert die Signale dreier Sub-Agenten zu einer Empfehlung, lässt Ollama
das Gesamtbild kommentieren und sendet EINE Telegram-Nachricht — aber nur bei
echten Zustandsänderungen (Anti-Spam via master_agent_log).

Sub-Agenten:
  1. NP-Flow      — letzter signal_log-Eintrag (signal_engine schreibt alle 60s)
  2. Neuron       — neuron_dissolve_snapshots + 7d-Delta (MM-08)
  3. Microstruktur — funding_rates, open_interest, CVD aus signal_log

State/Audit in eigener Tabelle master_agent_log — NICHT signal_log (würde
signal_engines last_score/last_regime-Logik korrumpieren).

Bekannte Vereinfachung: twap_active ist noch nicht verdrahtet (TWAP wird
nirgends persistiert — MM-06 ist nur eine Grafana-Query). Synthese behandelt
es als None. Follow-up: TWAP-Detektion in eine Tabelle schreiben.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp
import psycopg

import signal_engine as se

log = logging.getLogger(__name__)

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)

# Neuron-Signal-Schwellen (gespiegelt aus neuron_poller, in ICP)
NEURON_PRESSURE_RISING = 500_000
NEURON_RETREATING = -500_000

# Funding < diesem Wert (in %) → Shorts überfüllt → Squeeze-Risiko
SQUEEZE_FUNDING_PCT = -0.05

DAILY_SUMMARY_HOUR_UTC = 6      # täglicher Summary um 06:00 UTC
MIN_ALERT_COOLDOWN_MIN = 30     # Mindestabstand zwischen Alerts (Anti-Flapping)

# Trigger-Spalten/Details-Keys aus signal_log → Kurzlabel für active_triggers
_TRIGGER_FIELDS = {
    "trigger_mint": "mint", "trigger_wallet": "wallet_hop",
    "trigger_ob_thin": "ob_thin", "trigger_threshold": "sell_ratio",
}
_DETAIL_TRIGGER_KEYS = {
    "trigger_aggregator": "aggregator", "trigger_ls_skew": "ls_skew",
    "trigger_perp_spot": "perp_spot", "trigger_hop_spike": "hop_spike",
    "trigger_cvd_div": "cvd_div", "trigger_large_sell": "large_sell",
    "trigger_funding_spike": "funding_spike",
}


# ────────────────────────────────────────────────────────────────────────────
# Sub-Agent Reader
# ────────────────────────────────────────────────────────────────────────────
async def _read_np_flow(conn) -> dict:
    """Sub-Agent 1: letzter signal_log-Eintrag von signal_engine."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT score, regime, icp_price_usdt, details, "
            "trigger_mint, trigger_wallet, trigger_ob_thin, trigger_threshold "
            "FROM signal_log ORDER BY ts DESC LIMIT 1"
        )
        row = await cur.fetchone()
    if not row:
        return {"regime": "NEUTRAL", "score": 0, "monthly_flow_pct": None,
                "daily_flow_pct": None, "active_triggers": [], "icp_price": None}

    score, regime, price, details = row[0], row[1], row[2], (row[3] or {})
    triggers = []
    for col, label, val in zip(
        ("trigger_mint", "trigger_wallet", "trigger_ob_thin", "trigger_threshold"),
        ("mint", "wallet_hop", "ob_thin", "sell_ratio"),
        row[4:8],
    ):
        if val:
            triggers.append(label)
    for key, label in _DETAIL_TRIGGER_KEYS.items():
        if details.get(key):
            triggers.append(label)

    return {
        "regime": regime or "NEUTRAL",
        "score": score,
        "monthly_flow_pct": _as_float(details.get("post_mint_flow_pct")),
        "daily_flow_pct": _as_float(details.get("daily_flow_pct")),
        "active_triggers": triggers,
        "icp_price": _as_float(price),
        "cvd_net_1h": _as_float(details.get("cvd_net_1h")),
        "funding_rate_pct": _as_float(details.get("funding_rate_pct")),
    }


async def _read_neuron(conn) -> dict:
    """Sub-Agent 2: neuron_dissolve_snapshots + 7d-Delta auf dissolving_icp."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT dissolving_icp, dissolved_icp FROM neuron_dissolve_snapshots "
            "ORDER BY ts DESC LIMIT 1"
        )
        cur_row = await cur.fetchone()
        if not cur_row:
            return {"dissolving_icp": None, "dissolved_icp": None,
                    "delta_7d": None, "signal": "NEUTRAL"}
        dissolving, dissolved = cur_row[0], cur_row[1]

        await cur.execute(
            "SELECT dissolving_icp FROM neuron_dissolve_snapshots "
            "WHERE ts <= NOW() - INTERVAL '7 days' ORDER BY ts DESC LIMIT 1"
        )
        past_row = await cur.fetchone()

    delta_7d = None
    signal = "NEUTRAL"
    if past_row and past_row[0] is not None and dissolving is not None:
        delta_7d = dissolving - past_row[0]
        if delta_7d >= NEURON_PRESSURE_RISING:
            signal = "SUPPLY_PRESSURE_RISING"
        elif delta_7d <= NEURON_RETREATING:
            signal = "SUPPLY_RETREATING"
    return {"dissolving_icp": dissolving, "dissolved_icp": dissolved,
            "delta_7d": delta_7d, "signal": signal}


async def _read_micro(conn, np_ctx: dict) -> dict:
    """Sub-Agent 3: Microstruktur — Funding, OI-Delta, CVD-Trend, Squeeze-Risk."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT funding_rate FROM funding_rates ORDER BY ts DESC LIMIT 1"
        )
        row = await cur.fetchone()
        funding_rate = _as_float(row[0]) if row else None  # roh (z.B. -0.0001)

        # OI-Delta 1h: aktueller OI minus letzter Wert <= vor 1h
        await cur.execute("SELECT oi_icp FROM open_interest ORDER BY ts DESC LIMIT 1")
        row = await cur.fetchone()
        oi_now = _as_float(row[0]) if row else None
        await cur.execute(
            "SELECT oi_icp FROM open_interest "
            "WHERE ts <= NOW() - INTERVAL '1 hour' ORDER BY ts DESC LIMIT 1"
        )
        row = await cur.fetchone()
        oi_1h_ago = _as_float(row[0]) if row else None
    oi_delta_1h = (oi_now - oi_1h_ago) if (oi_now is not None and oi_1h_ago is not None) else None

    funding_pct = np_ctx.get("funding_rate_pct")  # bereits in % aus signal_log
    cvd_net = np_ctx.get("cvd_net_1h")
    if "cvd_div" in np_ctx["active_triggers"]:
        cvd_trend = "BEARISH_DIVERGENCE"
    elif cvd_net is not None and cvd_net > 0:
        cvd_trend = "BULLISH"
    elif cvd_net is not None and cvd_net < 0:
        cvd_trend = "BEARISH"
    else:
        cvd_trend = "NEUTRAL"

    squeeze_risk = "HIGH" if (funding_pct is not None and funding_pct < SQUEEZE_FUNDING_PCT) else "LOW"

    return {
        "cvd_trend": cvd_trend,
        "twap_active": None,                 # noch nicht verdrahtet (s. Modul-Docstring)
        "funding_rate_pct": funding_pct,
        "oi_delta_1h": oi_delta_1h,
        "squeeze_risk": squeeze_risk,
    }


# ────────────────────────────────────────────────────────────────────────────
# Synthese
# ────────────────────────────────────────────────────────────────────────────
def synthesize_context(np_ctx: dict, neuron_ctx: dict, micro_ctx: dict) -> dict:
    """Kombiniert die drei Sub-Agent-Outputs zu einer Empfehlung."""
    regime = np_ctx["regime"] or "NEUTRAL"

    conflicts = []
    if regime.startswith("KOMPRESSION") and micro_ctx["twap_active"] == "SELL":
        conflicts.append("NP_kompression_vs_sell_twap")
    if neuron_ctx["signal"] == "SUPPLY_PRESSURE_RISING" and regime.startswith("KOMPRESSION"):
        conflicts.append("neuron_supply_vs_np_kompression")

    bullish = 0
    bearish = 0

    # NP-Flow (höchste Gewichtung — historisch validiert)
    if regime in ("KOMPRESSION_STARK", "KOMPRESSION", "TRIGGER_BULLISH"):
        bullish += 3
    elif regime == "KOMPRESSION_SCHWACH":
        bullish += 1
    elif regime in ("DISTRIBUTION_STARK", "DISTRIBUTION_TOP"):
        bearish += 3
    elif regime == "DISTRIBUTION":
        bearish += 2

    # Neuron Dissolve (mittlere Gewichtung — strukturell)
    if neuron_ctx["signal"] == "SUPPLY_RETREATING":
        bullish += 2
    elif neuron_ctx["signal"] == "SUPPLY_PRESSURE_RISING":
        bearish += 2

    # Microstruktur (niedrige Gewichtung — kurzfristig)
    if micro_ctx["squeeze_risk"] == "HIGH":
        bullish += 1                          # Short-Squeeze-Setup
    if micro_ctx["twap_active"] == "SELL":
        bearish += 1
    if micro_ctx["cvd_trend"] == "BEARISH_DIVERGENCE":
        bearish += 1

    net = bullish - bearish
    if net >= 3:
        rec = "LONG_SETUP"
    elif net <= -3:
        rec = "SHORT_SETUP"
    elif net >= 1 and regime.startswith("KOMPRESSION"):
        rec = "LONG_SETUP_SCHWACH"
    else:
        rec = "WARTEN"

    return {"recommendation": rec, "bullish_score": bullish, "bearish_score": bearish,
            "conflicts": conflicts, "np": np_ctx, "neuron": neuron_ctx, "micro": micro_ctx}


# ────────────────────────────────────────────────────────────────────────────
# LLM + Telegram
# ────────────────────────────────────────────────────────────────────────────
async def _master_llm_comment(session: aiohttp.ClientSession, ctx: dict) -> str | None:
    """3–4 Satz LLM-Einschätzung des Gesamtbilds. None bei Fehler (silent fallback)."""
    if not se.OLLAMA_URL:
        return None
    np_, neu, mic = ctx["np"], ctx["neuron"], ctx["micro"]
    prompt = (
        "Du bist ein quantitativer ICP-Trader. Analysiere die folgenden Signale und gib "
        "eine präzise Einschätzung in 3-4 Sätzen auf Deutsch. Kein Markdown. Direkter Ton. "
        "Schließe mit einer konkreten Handlung ab (z.B. 'Einstieg prüfen wenn X', 'Warten bis Y', 'Exit wenn Z').\n\n"
        f"=== ON-CHAIN (NP-Flow) ===\n"
        f"Regime: {np_['regime']}\n"
        f"Monatlicher Exchange-Flow: {_fmt(np_['monthly_flow_pct'])}%\n"
        f"Tagesflow: {_fmt(np_['daily_flow_pct'])}%\n"
        f"Score: {np_['score']}/11 | Aktive Trigger: {', '.join(np_['active_triggers']) or 'keine'}\n\n"
        f"=== STRUKTUR (Neuron Dissolve) ===\n"
        f"Dissolving (im Countdown): {_fmt_icp(neu['dissolving_icp'])} ICP\n"
        f"Dissolved (verfügbar): {_fmt_icp(neu['dissolved_icp'])} ICP\n"
        f"Supply-Signal: {neu['signal']} | 7d-Delta: {_fmt_icp(neu['delta_7d'], signed=True)} ICP\n\n"
        f"=== MARKTMIKROSTRUKTUR ===\n"
        f"CVD-Trend: {mic['cvd_trend']}\n"
        f"Funding: {_fmt(mic['funding_rate_pct'], 4)}% | OI-Delta (1h): {_fmt_icp(mic['oi_delta_1h'], signed=True)} ICP\n"
        f"Squeeze-Risiko: {mic['squeeze_risk']}\n\n"
        f"=== SYNTHESE ===\n"
        f"Empfehlung: {ctx['recommendation']} | Bull {ctx['bullish_score']} / Bear {ctx['bearish_score']}\n"
        f"Konflikte: {', '.join(ctx['conflicts']) or 'keine'}\n\n"
        "Was bedeutet dieses Gesamtbild konkret? Worauf jetzt achten?"
    )
    try:
        async with session.post(
            f"{se.OLLAMA_URL}/api/generate",
            json={"model": se.OLLAMA_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.3, "num_predict": 220}},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                log.warning("master_agent llm: Ollama HTTP %s", resp.status)
                return None
            data = await resp.json()
            text = (data.get("response") or "").strip()
            return text or None
    except Exception as e:
        log.warning("master_agent llm: %s", e)
        return None


_REC_EMOJI = {"LONG_SETUP": "🟢", "LONG_SETUP_SCHWACH": "🟢", "SHORT_SETUP": "🔴", "WARTEN": "⚪"}


def _build_alert(ctx: dict, llm_text: str | None) -> str:
    np_, neu, mic = ctx["np"], ctx["neuron"], ctx["micro"]
    price = np_.get("icp_price")
    emoji = _REC_EMOJI.get(ctx["recommendation"], "🧠")
    conflict_line = (
        f"\n⚠️ KONFLIKT: {', '.join(ctx['conflicts'])}" if ctx["conflicts"] else ""
    )
    llm_block = f"\n\n─────────────\n📎 <i>{llm_text}</i>" if llm_text else ""
    return (
        f"🧠 <b>MIDAS BRAIN — {ctx['recommendation']}</b> {emoji}\n"
        f"ICP/USDT: <b>${_fmt(price)}</b>  |  Bull <b>{ctx['bullish_score']}</b> / Bear <b>{ctx['bearish_score']}</b>\n\n"
        f"📊 ON-CHAIN\n"
        f"  Regime: <b>{np_['regime']}</b> (monatl. Flow {_fmt(np_['monthly_flow_pct'])}%)\n"
        f"  Tages-Flow: {_fmt(np_['daily_flow_pct'])}%  |  Score {np_['score']}/11\n\n"
        f"🔓 NEURON DISSOLVE\n"
        f"  Im Countdown: {_fmt_icp(neu['dissolving_icp'])} ICP\n"
        f"  Verfügbar: {_fmt_icp(neu['dissolved_icp'])} ICP\n"
        f"  Signal: {neu['signal']} (7d {_fmt_icp(neu['delta_7d'], signed=True)} ICP)\n\n"
        f"⚡ MICROSTRUKTUR\n"
        f"  CVD: {mic['cvd_trend']}\n"
        f"  Funding: {_fmt(mic['funding_rate_pct'], 4)}%  |  OI-Δ(1h): {_fmt_icp(mic['oi_delta_1h'], signed=True)} ICP\n"
        f"  Squeeze-Risiko: {mic['squeeze_risk']}"
        f"{conflict_line}"
        f"{llm_block}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Feuer-Logik + State
# ────────────────────────────────────────────────────────────────────────────
async def _last_state(conn) -> dict | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT recommendation, regime, neuron_signal, ts, alerted "
            "FROM master_agent_log ORDER BY ts DESC LIMIT 1"
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {"recommendation": row[0], "regime": row[1], "neuron_signal": row[2],
            "ts": row[3], "alerted": row[4]}


async def _alert_cooldown_active(conn) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM master_agent_log WHERE alerted = true "
            f"AND ts >= NOW() - INTERVAL '{MIN_ALERT_COOLDOWN_MIN} minutes' LIMIT 1"
        )
        return await cur.fetchone() is not None


async def _daily_summary_due(conn) -> bool:
    """True wenn es 06:00 UTC ist und heute noch kein Alert raus ist."""
    if datetime.now(timezone.utc).hour != DAILY_SUMMARY_HOUR_UTC:
        return False
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM master_agent_log WHERE alerted = true "
            "AND ts >= date_trunc('day', NOW()) LIMIT 1"
        )
        return await cur.fetchone() is None


async def _write_state(conn, ctx: dict, alerted: bool) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO master_agent_log (
                recommendation, regime, neuron_signal,
                bullish_score, bearish_score, conflicts, alerted, details
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                ctx["recommendation"], ctx["np"]["regime"], ctx["neuron"]["signal"],
                ctx["bullish_score"], ctx["bearish_score"],
                ",".join(ctx["conflicts"]), alerted,
                json.dumps({"np": ctx["np"], "neuron": ctx["neuron"], "micro": ctx["micro"]}, default=str),
            ),
        )
    await conn.commit()


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────
def _as_float(v):
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _fmt(v, decimals: int = 1) -> str:
    return f"{v:.{decimals}f}" if isinstance(v, (int, float)) else "n/a"


def _fmt_icp(v, signed: bool = False) -> str:
    if not isinstance(v, (int, float)):
        return "n/a"
    return f"{v:+,.0f}" if signed else f"{v:,.0f}"


# ────────────────────────────────────────────────────────────────────────────
# Run
# ────────────────────────────────────────────────────────────────────────────
async def run() -> None:
    log.info("master_agent: start")
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        np_ctx = await _read_np_flow(conn)
        neuron_ctx = await _read_neuron(conn)
        micro_ctx = await _read_micro(conn, np_ctx)
        ctx = synthesize_context(np_ctx, neuron_ctx, micro_ctx)

        last = await _last_state(conn)
        changed = (
            last is None
            or last["recommendation"] != ctx["recommendation"]
            or last["regime"] != ctx["np"]["regime"]
            or last["neuron_signal"] != ctx["neuron"]["signal"]
        )
        summary_due = await _daily_summary_due(conn)
        cooldown = await _alert_cooldown_active(conn)
        quiet = se._is_quiet_hours()

        should_alert = (changed or summary_due) and not cooldown and not quiet

        log.info(
            "master_agent: rec=%s bull=%d bear=%d regime=%s neuron=%s "
            "changed=%s summary_due=%s cooldown=%s quiet=%s -> alert=%s",
            ctx["recommendation"], ctx["bullish_score"], ctx["bearish_score"],
            ctx["np"]["regime"], ctx["neuron"]["signal"],
            changed, summary_due, cooldown, quiet, should_alert,
        )

        if should_alert:
            async with aiohttp.ClientSession() as session:
                llm_text = await _master_llm_comment(session, ctx)
                await se._send_telegram(session, _build_alert(ctx, llm_text))

        await _write_state(conn, ctx, alerted=should_alert)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

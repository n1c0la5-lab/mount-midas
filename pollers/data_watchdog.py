"""
data_watchdog.py — Datenqualitäts-Wächter
Prüft alle 5 Minuten ob jede Datenquelle frisch ist.
Schreibt Status in system_health. Telegram-Alert bei Staleness (max 1x/Stunde pro Quelle).
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
import psycopg

log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)

_CREATE = """
CREATE TABLE IF NOT EXISTS system_health (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source        TEXT        NOT NULL,
    last_data_ts  TIMESTAMPTZ,
    age_minutes   NUMERIC,
    is_stale      BOOLEAN     NOT NULL DEFAULT FALSE,
    threshold_min INTEGER     NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_system_health_source_ts ON system_health (source, ts DESC);
"""

# Quelle → (SQL für MAX(ts), Stale-Schwelle in Minuten)
#
# Zwei Kategorien:
#   Poller-Tabellen:  werden bei jedem Lauf beschrieben → enge Schwelle (5–90min)
#   Event-Tabellen:   werden nur bei Marktereignissen beschrieben → weite Schwelle
#     liquidation_events: ICP kann stundenlang ohne Liq-Events sein — 8h Schwelle
SOURCES: dict[str, tuple[str, int]] = {
    "spot_trades":        ("SELECT MAX(ts) FROM spot_trades",              5),
    "ob_snapshots":       ("SELECT MAX(ts) FROM ob_snapshots",             5),
    "open_interest":      ("SELECT MAX(ts) FROM open_interest",           90),
    "funding_rates":      ("SELECT MAX(ts) FROM funding_rates",           90),
    "liquidation_events": ("SELECT MAX(ts) FROM liquidation_events",  8 * 60),
    "signal_log":         ("SELECT MAX(ts) FROM signal_log",               5),
    "volume_profile":     ("SELECT MAX(calculated_at) FROM volume_profile", 26 * 60),
}

# Throttle: letzter Alert-Zeitpunkt pro Quelle (in-memory, resets bei Container-Neustart)
_last_alert: dict[str, datetime] = {}
_ALERT_COOLDOWN_MIN = 60


async def _send_telegram(session: aiohttp.ClientSession, text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with session.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                log.info("data_watchdog: Telegram alert sent")
            else:
                log.warning("data_watchdog: Telegram error %s", await resp.text())
    except Exception as e:
        log.warning("data_watchdog: Telegram send failed: %s", e)


def _should_alert(source: str) -> bool:
    last = _last_alert.get(source)
    if last is None:
        return True
    age = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return age >= _ALERT_COOLDOWN_MIN


async def run() -> None:
    log.info("data_watchdog: start")
    now = datetime.now(timezone.utc)
    stale_sources: list[str] = []

    try:
        async with await psycopg.AsyncConnection.connect(_DSN) as conn:
            await conn.execute(_CREATE)

            for source, (query, threshold_min) in SOURCES.items():
                row = await (await conn.execute(query)).fetchone()
                last_ts = row[0] if row else None

                if last_ts is None:
                    age_min = None
                    is_stale = True
                else:
                    age_min = round((now - last_ts).total_seconds() / 60, 1)
                    is_stale = age_min > threshold_min

                await conn.execute(
                    """
                    INSERT INTO system_health (source, last_data_ts, age_minutes, is_stale, threshold_min)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (source, last_ts, age_min, is_stale, threshold_min),
                )

                if is_stale:
                    stale_sources.append(source)
                    age_str = f"{age_min:.0f}min" if age_min is not None else "keine Daten"
                    log.warning("data_watchdog: STALE — %s (age=%s, threshold=%dmin)",
                                source, age_str, threshold_min)
                else:
                    log.info("data_watchdog: OK — %s (age=%.1fmin)", source, age_min)

            await conn.commit()

        if stale_sources:
            async with aiohttp.ClientSession() as session:
                for source in stale_sources:
                    if _should_alert(source):
                        age_min = None
                        for s, (q, _) in SOURCES.items():
                            if s == source:
                                break
                        msg = (
                            f"⚠️ <b>MOUNT MIDAS — Datenquelle stale</b>\n"
                            f"Quelle: <code>{source}</code>\n"
                            f"Schwelle: {SOURCES[source][1]} min überschritten"
                        )
                        await _send_telegram(session, msg)
                        _last_alert[source] = now

    except Exception:
        log.exception("data_watchdog: Fehler")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

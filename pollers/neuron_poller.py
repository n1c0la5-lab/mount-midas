"""
Mount Midas — Neuron Dissolve Poller (MM-08)

Trackt den strukturellen ICP-Supply aus den NNS-Governance-Metriken:
wie viel ICP aufgelöst (dissolved), aktiv im Countdown (dissolving) oder
kurzfristig gesperrt (not_dissolving) ist. Liefert den Wochen-Vorlauf, den
NP-Flow und Microstruktur nicht sehen.

Hauptsignal: Trend/Delta auf `dissolving_icp` — wenn große Mengen ICP von
NotDissolving → Dissolving wechseln, beginnt struktureller Verkaufsdruck,
Wochen bevor das ICP liquide wird.

Datenquelle: IC Dashboard governance-metrics (Prometheus-Style Aggregate).
7d/30d-Zeitbuckets sind über die API nicht verfügbar — feinste Zeitauflösung
ist "<6 Monate". Daher state-basiertes Schema (s. MM-08 Spec).
"""
import asyncio
import logging
import os

import aiohttp
import psycopg

log = logging.getLogger(__name__)

GOV_METRICS_URL = "https://ic-api.internetcomputer.org/api/v3/governance-metrics"

# Schwellen für das Supply-Signal (delta_7d auf dissolving_icp, in ICP).
# Erste Schätzungen — nach 2 Wochen Live-Daten kalibrieren.
PRESSURE_RISING_THRESHOLD = 500_000      # +500k ICP/Woche neu im Countdown
RETREATING_THRESHOLD = -500_000          # -500k ICP/Woche raus aus Countdown

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)

# Welche Governance-Metriken wir ziehen → Snapshot-Spalte.
# Werte stehen in e8s; _scalar() rechnet in ganze ICP um.
_METRIC_MAP = {
    "governance_dissolved_neurons_e8s": "dissolved_icp",
    "governance_dissolving_neurons_e8s": "dissolving_icp",
    "governance_not_dissolving_neurons_e8s": "not_dissolving_icp",
    "governance_neurons_with_less_than_6_months_dissolve_delay_e8s": "lt_6mo_icp",
    "governance_total_locked_e8s": "total_locked_icp",
    "governance_total_staked_e8s": "total_staked_icp",
}
# Zähler-Metriken (keine e8s-Umrechnung).
_COUNT_MAP = {
    "governance_dissolving_neurons_count": "dissolving_count",
    "governance_neurons_total": "neuron_count",
}


async def _fetch_metrics() -> dict[str, float]:
    """Holt governance-metrics und flacht sie zu {name: value} ab."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            GOV_METRICS_URL, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

    out: dict[str, float] = {}
    for m in data.get("metrics", []):
        subsets = m.get("subsets") or []
        if not subsets:
            continue
        # value = [timestamp, "stringwert"] — wir nehmen das erste Subset.
        try:
            out[m["name"]] = float(subsets[0]["value"][1])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return out


def _build_snapshot(metrics: dict[str, float]) -> dict[str, int | None]:
    """Mappt die rohen Metriken auf die Snapshot-Spalten."""
    snap: dict[str, int | None] = {}
    for metric_name, col in _METRIC_MAP.items():
        v = metrics.get(metric_name)
        snap[col] = int(v / 1e8) if v is not None else None
    for metric_name, col in _COUNT_MAP.items():
        v = metrics.get(metric_name)
        snap[col] = int(v) if v is not None else None
    return snap


async def _insert(snap: dict[str, int | None]) -> None:
    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO neuron_dissolve_snapshots (
                    dissolved_icp, dissolving_icp, not_dissolving_icp,
                    lt_6mo_icp, total_locked_icp, total_staked_icp,
                    dissolving_count, neuron_count
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    snap["dissolved_icp"],
                    snap["dissolving_icp"],
                    snap["not_dissolving_icp"],
                    snap["lt_6mo_icp"],
                    snap["total_locked_icp"],
                    snap["total_staked_icp"],
                    snap["dissolving_count"],
                    snap["neuron_count"],
                ),
            )
        await conn.commit()


async def _compute_signal(current_dissolving: int | None) -> tuple[str, int | None]:
    """
    Vergleicht dissolving_icp mit dem Stand vor ~7 Tagen und leitet das
    Supply-Signal ab. Gibt (signal, delta_7d) zurück.
    """
    if current_dissolving is None:
        return "NEUTRAL", None

    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT dissolving_icp
                FROM neuron_dissolve_snapshots
                WHERE ts <= NOW() - INTERVAL '7 days'
                ORDER BY ts DESC
                LIMIT 1
                """
            )
            row = await cur.fetchone()

    if row is None or row[0] is None:
        return "NEUTRAL", None  # noch keine 7-Tage-Historie

    delta_7d = current_dissolving - row[0]
    if delta_7d >= PRESSURE_RISING_THRESHOLD:
        signal = "SUPPLY_PRESSURE_RISING"
    elif delta_7d <= RETREATING_THRESHOLD:
        signal = "SUPPLY_RETREATING"
    else:
        signal = "NEUTRAL"
    return signal, delta_7d


async def run() -> None:
    log.info("neuron_poller: start")
    metrics = await _fetch_metrics()
    if not metrics:
        log.error("neuron_poller: keine Metriken erhalten — Abbruch")
        return

    snap = _build_snapshot(metrics)
    if snap["dissolving_icp"] is None or snap["dissolved_icp"] is None:
        log.error("neuron_poller: Kernmetriken fehlen — Abbruch (%s)", snap)
        return

    await _insert(snap)
    signal, delta_7d = await _compute_signal(snap["dissolving_icp"])

    log.info(
        "neuron_poller: dissolved=%s ICP, dissolving=%s ICP (%s neurons), "
        "signal=%s, delta_7d=%s",
        f"{snap['dissolved_icp']:,}",
        f"{snap['dissolving_icp']:,}",
        snap["dissolving_count"],
        signal,
        f"{delta_7d:+,}" if delta_7d is not None else "n/a",
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

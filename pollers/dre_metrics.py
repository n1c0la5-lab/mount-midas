"""
dre_metrics.py — ICP Node-Provider Metrics via DRE CLI
Fetches monthly reward + per-node performance data for all NPs.
Schedule: daily 04:30 UTC (after np_poller 04:00)
"""
import asyncio
import csv
import logging
import os
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

DRE_BIN = os.environ.get("DRE_BIN", "/home/hess/.local/bin/dre")

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)


# ── DRE CLI ───────────────────────────────────────────────────────────────────

async def fetch_period_csvs(month: str, output_dir: Path) -> Path | None:
    """Runs dre node-rewards past-rewards MONTH, returns the output subdirectory."""
    result = await asyncio.to_thread(
        subprocess.run,
        [DRE_BIN, "node-rewards", "past-rewards", month,
         "--csv-detailed-output-path", str(output_dir)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("dre failed for %s: %s", month, result.stderr[-300:])
        return None
    subdirs = [d for d in output_dir.iterdir() if d.is_dir()]
    if not subdirs:
        log.warning("dre_metrics: no output folder found for %s", month)
        return None
    return subdirs[0]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_day(day_str: str) -> datetime:
    """Parse DRE day_utc (e.g. '2026-3-15', no zero-padding)."""
    parts = day_str.split("-")
    return datetime(int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=timezone.utc)


def _float_or_none(val: str) -> float | None:
    try:
        return float(val) if val.strip() else None
    except (ValueError, AttributeError):
        return None


def _int_or_none(val: str) -> int | None:
    try:
        return int(val) if val.strip() else None
    except (ValueError, AttributeError):
        return None


# ── DB helpers ────────────────────────────────────────────────────────────────

async def is_period_done(conn, reward_period: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM np_reward_mints WHERE reward_period = %s LIMIT 1",
            (reward_period,),
        )
        return await cur.fetchone() is not None


async def insert_reward_mints(conn, period_dir: Path, reward_period: str) -> int:
    summary = period_dir / "node_providers_summary.csv"
    if not summary.exists():
        log.warning("dre_metrics: node_providers_summary.csv not found in %s", period_dir)
        return 0

    rows: list[tuple] = []
    with summary.open() as f:
        for row in csv.DictReader(f):
            rewards_icp = _float_or_none(row.get("rewards_icp", ""))
            if rewards_icp is None:
                continue
            rows.append((
                datetime.now(timezone.utc),
                row["node_provider_id"],
                rewards_icp,
                reward_period,
            ))

    async with conn.cursor() as cur:
        for r in rows:
            await cur.execute(
                """
                INSERT INTO np_reward_mints (ts, provider_principal, amount_icp, reward_period)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (provider_principal, reward_period) DO NOTHING
                """,
                r,
            )
    await conn.commit()
    return len(rows)


async def insert_np_performance(conn, period_dir: Path) -> int:
    total = 0
    for provider_dir in sorted(period_dir.iterdir()):
        if not provider_dir.is_dir():
            continue
        metrics_file = provider_dir / "node_metrics_by_day.csv"
        if not metrics_file.exists():
            continue

        provider_principal = provider_dir.name
        rows: list[tuple] = []

        with metrics_file.open() as f:
            for row in csv.DictReader(f):
                try:
                    ts = _parse_day(row["day_utc"])
                except (KeyError, ValueError, IndexError):
                    continue

                failure_rate = _float_or_none(row.get("original_failure_rate", ""))
                perf_mult = _float_or_none(row.get("performance_multiplier", ""))

                rows.append((
                    ts,
                    row["node_id"],
                    provider_principal,
                    _int_or_none(row.get("num_blocks_proposed", "")),
                    _int_or_none(row.get("num_blocks_failed", "")),
                    failure_rate * 100 if failure_rate is not None else None,
                    perf_mult * 100 if perf_mult is not None else None,
                ))

        async with conn.cursor() as cur:
            for r in rows:
                await cur.execute(
                    """
                    INSERT INTO np_performance
                        (ts, node_id, provider_principal, blocks_produced, blocks_failed,
                         failure_rate_pct, uptime_pct)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ts, node_id) DO NOTHING
                    """,
                    r,
                )
        await conn.commit()
        total += len(rows)

    return total


# ── Main ──────────────────────────────────────────────────────────────────────

async def run() -> None:
    log.info("dre_metrics: start")

    today = date.today()
    # Check current month and previous month — reward periods may span calendar boundaries
    months: list[str] = []
    y, m = today.year, today.month
    for _ in range(2):
        months.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1

    async with await psycopg.AsyncConnection.connect(_DSN) as conn:
        for month in months:
            if await is_period_done(conn, month):
                log.info("dre_metrics: %s already stored, skipping", month)
                continue

            log.info("dre_metrics: fetching %s", month)
            with tempfile.TemporaryDirectory() as tmpdir:
                period_dir = await fetch_period_csvs(month, Path(tmpdir))
                if not period_dir:
                    continue

                n_mints = await insert_reward_mints(conn, period_dir, month)
                n_perf = await insert_np_performance(conn, period_dir)
                log.info("dre_metrics: %s — %d providers, %d performance rows", month, n_mints, n_perf)

    log.info("dre_metrics: done")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

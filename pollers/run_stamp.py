"""
run_stamp.py — MM-10 Datenwächter, Schicht A: Lauf-Stempel

Jeder Poller-Lauf hinterlässt eine Zeile in poller_runs: lief er, wie lange,
wie viele Zeilen hat er GEMESSEN geschrieben, gab es einen Fehler.

Warum das nötig ist: system_health prüft nur max(ts) der Zieltabelle. Damit
lässt sich "Poller kaputt" nicht von "Markt still" unterscheiden — und ein
Poller, der brav läuft und stillschweigend nichts tut, fällt gar nicht auf.
Genau so blieb dre_metrics 12 Tage unentdeckt (MM-10 Defekt 1).

rows_written ist absichtlich NULL, wenn ein Poller keine Zahl zurückgibt.
NULL heißt "nicht gemeldet", nicht "0" — eine geratene 0 wäre schlimmer als
eine ehrliche Lücke.
"""
import logging
import os
from datetime import datetime, timezone

import psycopg

log = logging.getLogger(__name__)

_DSN = (
    f"host={os.environ['DB_HOST']} "
    f"port={os.environ.get('DB_PORT', '5432')} "
    f"dbname={os.environ['DB_NAME']} "
    f"user={os.environ['DB_USER']} "
    f"password={os.environ['DB_PASSWORD']}"
)

# Das Schema gehört Migration 018, NICHT dieser Datei.
#
# Hier stand bis 2026-07-27 ein CREATE TABLE/INDEX IF NOT EXISTS, das bei JEDEM
# Stempel mitlief. Seriell war das bloss überflüssig. Seit der runner nebenläufig
# ist (PR #18), verklemmen sich zwei gleichzeitige Stempel daran:
# CREATE INDEX IF NOT EXISTS nimmt einen ShareLock, das folgende INSERT einen
# RowExclusiveLock — die beiden vertragen sich nicht, und zwei Poller, die sich
# überkreuzen, laufen in einen Deadlock. In den ersten zwei Stunden nach dem
# Deploy gingen so fünf Stempel verloren.
#
# Ein verlorener Stempel ist teurer, als er aussieht: der Poller lief, aber
# Schicht A sieht ihn nicht — also ausgerechnet die Wache, die "läuft der
# Poller?" beantworten soll. Der Schreibpfad einer Wache darf nicht selbst die
# Lücken erzeugen, die sie melden soll.
#
# Dass das Schema wirklich da ist, sichern migrate.sh und die Schema-Drift-
# Prüfung in gate.sh — nicht ein DDL im heissen Pfad.

_MAX_ERROR_LEN = 2000


async def record_run(
    poller: str,
    started_at: datetime,
    finished_at: datetime,
    rows_written: int | None,
    ok: bool,
    error: str | None = None,
) -> None:
    """
    Schreibt einen Lauf-Stempel. Schluckt eigene Fehler bewusst: der Wächter
    darf den Poller, den er beobachtet, niemals zum Absturz bringen.
    """
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    try:
        async with await psycopg.AsyncConnection.connect(_DSN) as conn:
            await conn.execute(
                """
                INSERT INTO poller_runs
                    (poller, started_at, finished_at, duration_ms, rows_written, ok, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (poller, started_at, finished_at, duration_ms, rows_written, ok,
                 error[:_MAX_ERROR_LEN] if error else None),
            )
            await conn.commit()
    except Exception as e:
        log.warning("run_stamp: Stempel für %s fehlgeschlagen: %s", poller, e)


def now() -> datetime:
    return datetime.now(timezone.utc)

#!/usr/bin/env python3
"""
regression_check.py — MM-10 Gate

Prüft die Defektklassen, die am 26.07.2026 gefunden wurden, damit keine davon
zurückkehrt. Regel aus TrendRider: jeder mechanisch prüfbare Fund wird zum
Gate befördert. Was nur durch Urteil auffällt, gehört in die Struktur-Analyse.

Zwei Arten von Prüfungen:

  STATISCH  — lesen nur den Quellcode, laufen immer und überall.
  MIT DB    — brauchen eine leere Postgres-Datenbank (--dsn), in die die
              Migrationen frisch eingespielt werden. Ohne --dsn werden sie
              als übersprungen gemeldet, nicht als grün.

Der Unterschied ist wichtig: ein übersprungener Test darf nie wie ein
bestandener aussehen.

Aufruf:
    python3 scripts/regression_check.py
    python3 scripts/regression_check.py --dsn "postgresql://user:pw@host:5432/mm_test"
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
POLLERS = REPO / "pollers"
MIGRATIONS = REPO / "migrations"

# Tabellen, die bewusst NICHT vom Wächter beobachtet werden, mit Begründung.
# Wer hier etwas einträgt, muss den Grund nennen — eine leere Ausnahme ist
# genau die Lücke, die MM-10 geschlossen hat.
WATCHDOG_EXEMPT: dict[str, str] = {}

# Tabellen, die über dynamisches SQL beschrieben werden (Tabellenname als
# Variable), weshalb die INSERT-INTO-Suche sie nicht findet. Pro Poller-Datei
# deklariert. Wer eine Datei mit dynamischem INSERT hinzufügt, ohne sie hier
# einzutragen, fällt durch — sonst wäre diese Liste ein Loch in der
# Vollständigkeits-Prüfung statt einer Ergänzung.
DYNAMIC_WRITES: dict[str, set[str]] = {
    "tick_collector.py": {"spot_trades", "perp_trades"},
}


class Result:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.skipped: list[str] = []
        self.passed: list[str] = []

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"✅ {name}")

    def fail(self, name: str, detail: str) -> None:
        self.failures.append(name)
        print(f"❌ {name}")
        for line in detail.rstrip().splitlines():
            print(f"     {line}")

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"⏭️  {name} — übersprungen: {why}")


# ── Statische Prüfungen ───────────────────────────────────────────────────────

def _poller_written_tables() -> tuple[dict[str, set[str]], list[str]]:
    """
    Tabelle → Menge der Poller-Dateien, die per INSERT hineinschreiben.

    Zweiter Rückgabewert: Dateien mit dynamischem INSERT (Tabellenname als
    Variable), die nicht in DYNAMIC_WRITES deklariert sind.
    """
    tables: dict[str, set[str]] = {}
    undeclared: list[str] = []
    for py in sorted(POLLERS.glob("*.py")):
        text = py.read_text()
        for m in re.finditer(r"INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)", text, re.I):
            tables.setdefault(m.group(1).lower(), set()).add(py.name)
        if re.search(r"INSERT\s+INTO\s+\{", text, re.I):
            declared = DYNAMIC_WRITES.get(py.name)
            if declared is None:
                undeclared.append(py.name)
            else:
                for t in declared:
                    tables.setdefault(t, set()).add(f"{py.name} (dynamisch)")
    return tables, undeclared


def _watchdog_sources() -> set[str]:
    """Die Schlüssel von data_watchdog.SOURCES, statisch aus dem Quelltext."""
    text = (POLLERS / "data_watchdog.py").read_text()
    block = text.split("SOURCES: dict[str, Source] = {", 1)
    if len(block) != 2:
        raise SystemExit("regression_check: SOURCES-Block in data_watchdog.py nicht gefunden")
    body = block[1].split("\n}", 1)[0]
    return {m.group(1) for m in re.finditer(r'^\s*"([a-z_]+)":\s*Source\(', body, re.M)}


def check_watchdog_completeness(r: Result) -> None:
    """
    Jede von einem Poller geschriebene Tabelle muss der Wächter kennen.

    Das ist der Kern-Regressionsschutz: der ursprüngliche Defekt war, dass 7 von
    25 Tabellen bewacht waren und die stillen Täglichen fehlten. Ohne diese
    Prüfung fällt jede neu hinzugefügte Tabelle wieder durchs Netz — dieselbe
    Klasse wie die Janus-_MARKER_KEYS-Falle in TrendRider.
    """
    name = "Wächter-Vollständigkeit: jede poller-geschriebene Tabelle ist bewacht"
    written, undeclared = _poller_written_tables()
    watched = _watchdog_sources()

    if undeclared:
        r.fail(name, "Dynamischer INSERT (Tabellenname als Variable), aber nicht in "
                     "DYNAMIC_WRITES deklariert: " + ", ".join(undeclared))
        return

    missing = {t: sorted(v) for t, v in written.items()
               if t not in watched and t not in WATCHDOG_EXEMPT}
    if missing:
        detail = "\n".join(f"{t} — geschrieben von {', '.join(v)}" for t, v in sorted(missing.items()))
        r.fail(name, f"Nicht in data_watchdog.SOURCES:\n{detail}")
        return

    # Gegenrichtung: eine bewachte Tabelle, die niemand schreibt, ist ein
    # Tippfehler oder toter Code — der Wächter wäre dort dauerhaft rot.
    orphan = sorted(watched - set(written) - set(WATCHDOG_EXEMPT))
    if orphan:
        r.fail(name, "Bewacht, aber von keinem Poller beschrieben: " + ", ".join(orphan))
        return

    r.ok(f"{name} ({len(watched)} Quellen)")


def check_poller_names_match_runner(r: Result) -> None:
    """
    Die Namen in POLLER_CADENCE müssen genau die sein, die runner.py stempelt.

    runner.py stempelt '<modul>.<funktion>'. Ein Tippfehler hier hieße: der
    Wächter wartet auf einen Poller, den es nicht gibt (dauerhaft rot), oder
    er beobachtet einen echten Poller gar nicht (stumm) — beides schlimmer als
    kein Check.
    """
    name = "Lauf-Wache: POLLER_CADENCE deckt sich mit runner.py"
    runner = (POLLERS / "runner.py").read_text()

    # Nur WIEDERKEHRENDE Läufe brauchen einen Takt. Erstlauf-nur-Poller wie
    # tick_collector.run_ohlcv_backfill haben keine Kadenz — für sie eine zu
    # fordern hieße, den Wächter dauerhaft rot zu stellen.
    scheduled = {
        f"{m.group(1)}.{m.group(2)}"
        for m in re.finditer(
            r"schedule\.every\([^)]*\)[^\n]*?\.do\(\s*run_async\(\s*([a-z_]+)\.([a-z_]+)\s*\)",
            runner,
        )
    }
    if not scheduled:
        r.fail(name, "keine schedule.every(...).do(run_async(...))-Zeilen in runner.py gefunden — "
                     "die Extraktion ist kaputt, nicht der Code")
        return

    wd = (POLLERS / "data_watchdog.py").read_text()
    cad_block = wd.split("POLLER_CADENCE: dict[str, int] = {", 1)
    if len(cad_block) != 2:
        r.fail(name, "POLLER_CADENCE-Block nicht gefunden")
        return
    cadence = {m.group(1) for m in re.finditer(r'^\s*"([a-z_.]+)":', cad_block[1].split("\n}", 1)[0], re.M)}

    problems = []
    unwatched = sorted(scheduled - cadence)
    if unwatched:
        problems.append("Von runner.py gestempelt, aber nicht in POLLER_CADENCE: "
                        + ", ".join(unwatched))
    phantom = sorted(cadence - scheduled)
    if phantom:
        problems.append("In POLLER_CADENCE, aber von runner.py nie gestempelt: "
                        + ", ".join(phantom))
    if problems:
        r.fail(name, "\n".join(problems))
        return
    r.ok(f"{name} ({len(cadence)} Poller)")


def check_no_unnamed_index_with_if_not_exists(r: Result) -> None:
    """
    'CREATE INDEX IF NOT EXISTS ON tabelle (...)' ist ein Syntaxfehler — mit
    IF NOT EXISTS ist der Indexname Pflicht. Vier solche Statements standen in
    Migration 002 und haben ein Frisch-Setup ab Zeile 19 abgebrochen.

    (Ohne IF NOT EXISTS ist 'CREATE INDEX ON ...' gültig — Postgres generiert
    dann einen Namen. Das ist hier ausdrücklich nicht gemeint.)
    """
    name = "Migrationen: kein CREATE INDEX IF NOT EXISTS ohne Namen"
    hits = []
    pattern = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+ON\s", re.I)
    for sql in sorted(MIGRATIONS.glob("*.sql")):
        for i, line in enumerate(sql.read_text().splitlines(), 1):
            code = line.split("--", 1)[0]   # SQL-Kommentare ignorieren
            if pattern.search(code):
                hits.append(f"{sql.name}:{i}: {line.strip()}")
    if hits:
        r.fail(name, "\n".join(hits))
        return
    r.ok(name)


def check_rowcount_not_guessed(r: Result) -> None:
    """
    Ein Upsert mit ON CONFLICT darf nicht len(...) als geschriebene Zeilenzahl
    zurückgeben — DO NOTHING schluckt Duplikate still. Genau deshalb meldete
    wallet_tracker stündlich bitgleich '2444 new' bei stillstehender Tabelle.

    Geprüft wird: enthält eine Funktion ein ON CONFLICT, darf sie nicht mit
    'return len(' enden.
    """
    name = "Upserts melden gemessene, nicht geratene Zeilenzahlen"
    hits = []
    for py in sorted(POLLERS.glob("*.py")):
        text = py.read_text()
        # Funktionsblöcke grob am 'def ' trennen
        for block in re.split(r"\n(?=(?:async )?def )", text):
            if re.search(r"ON\s+CONFLICT", block, re.I) and re.search(r"return\s+len\(", block):
                fn = re.match(r"(?:async )?def\s+([a-zA-Z_]+)", block.strip())
                hits.append(f"{py.name}: {fn.group(1) if fn else '?'}() — "
                            f"ON CONFLICT und return len(...)")
    if hits:
        r.fail(name, "\n".join(hits))
        return
    r.ok(name)


def check_period_completeness_logic(r: Result) -> None:
    """
    Der laufende Monat darf nie als 'fertig' gelten, und ein abgeschlossener
    Monat erst dann, wenn er NACH seinem Ende gelesen wurde.

    Geprüft wird die reine Datumslogik (_month_end) plus die Bedingung im
    Quelltext — ohne DB, damit dieser Kern-Defekt überall geprüft wird.
    """
    name = "dre_metrics: Monats-Vollständigkeit (Verhalten, nicht Wortwahl)"
    text = (POLLERS / "dre_metrics.py").read_text()

    # Die reinen Funktionen aus dem Quelltext holen und AUSFÜHREN. dre_metrics
    # zu importieren geht nicht (braucht psycopg + DB-Umgebungsvariablen), und
    # nur nach Schlüsselwörtern im Rumpf zu greppen wäre wertlos: ein früherer
    # Entwurf dieser Prüfung blieb grün, als die Monatsende-Bedingung durch
    # "if False:" ersetzt wurde, weil das Wort _month_end noch dastand.
    from datetime import datetime, timezone
    ns: dict = {"datetime": datetime, "timezone": timezone}
    for fname in ("_month_end", "is_period_complete"):
        m = re.search(rf"^def {fname}\(.*?(?=\n\n\n|\n\nasync def |\n\ndef )",
                      text, re.S | re.M)
        if not m:
            r.fail(name, f"{fname}() nicht gefunden — muss eine reine, "
                         f"testbare Funktion auf Modulebene sein")
            return
        try:
            exec(m.group(0), ns)
        except Exception as e:
            r.fail(name, f"{fname}() nicht ausführbar: {e}")
            return

    month_end = ns["_month_end"]
    complete = ns["is_period_complete"]

    for period, expected in [("2026-07", datetime(2026, 8, 1, tzinfo=timezone.utc)),
                             ("2026-12", datetime(2027, 1, 1, tzinfo=timezone.utc)),
                             ("2026-01", datetime(2026, 2, 1, tzinfo=timezone.utc))]:
        got = month_end(period)
        if got != expected:
            r.fail(name, f"_month_end({period!r}) = {got}, erwartet {expected}")
            return

    U = timezone.utc
    # (Periode, jetzt, letzter fetch, erwartet, warum)
    cases = [
        ("2026-07", datetime(2026, 7, 26, tzinfo=U), None,
         False, "laufender Monat, nie gelesen"),
        ("2026-07", datetime(2026, 7, 26, tzinfo=U), datetime(2026, 7, 15, tzinfo=U),
         False, "laufender Monat, mitten drin gelesen — DER Ur-Defekt"),
        ("2026-07", datetime(2026, 7, 31, 23, 59, tzinfo=U), datetime(2026, 7, 31, tzinfo=U),
         False, "letzter Tag des Monats zählt noch als laufend"),
        ("2026-07", datetime(2026, 8, 5, tzinfo=U), datetime(2026, 7, 15, tzinfo=U),
         False, "abgeschlossen, aber nur mitten drin gelesen — muss nachgeholt werden"),
        ("2026-07", datetime(2026, 8, 5, tzinfo=U), None,
         False, "abgeschlossen, nie gelesen"),
        ("2026-07", datetime(2026, 8, 5, tzinfo=U), datetime(2026, 8, 1, tzinfo=U),
         True,  "abgeschlossen und nach Monatsende gelesen"),
        ("2026-12", datetime(2027, 1, 3, tzinfo=U), datetime(2027, 1, 2, tzinfo=U),
         True,  "Jahreswechsel"),
        ("2026-12", datetime(2026, 12, 20, tzinfo=U), datetime(2026, 12, 19, tzinfo=U),
         False, "Jahreswechsel, laufender Dezember"),
    ]
    for period, now, fetched, expected, why in cases:
        got = complete(period, now, fetched)
        if got != expected:
            r.fail(name, f"is_period_complete({period!r}, now={now:%Y-%m-%d}, "
                         f"fetched={fetched}) = {got}, erwartet {expected}\n"
                         f"Fall: {why}")
            return

    r.ok(f"{name} ({len(cases)} Fälle)")


# ── Prüfungen mit Datenbank ───────────────────────────────────────────────────

def check_migrations_and_queries(r: Result, dsn: str | None) -> None:
    name_mig = "Migrationen laufen auf leerer DB durch"
    name_q   = "Wächter-Queries laufen gegen das Migrations-Schema"

    if not dsn:
        r.skip(name_mig, "kein --dsn angegeben")
        r.skip(name_q, "kein --dsn angegeben")
        return

    try:
        import psycopg
    except ImportError:
        r.skip(name_mig, "psycopg nicht installiert")
        r.skip(name_q, "psycopg nicht installiert")
        return

    files = sorted(MIGRATIONS.glob("*.sql"))
    with psycopg.connect(dsn, autocommit=True) as conn:
        for sql in files:
            try:
                conn.execute(sql.read_text())
            except Exception as e:
                r.fail(name_mig, f"{sql.name}: {e}")
                r.skip(name_q, "Migrationen fehlgeschlagen")
                return
        r.ok(f"{name_mig} ({len(files)} Dateien)")

        # Die Queries werden aus dem Quelltext gelesen, nicht abgeschrieben —
        # sonst prüft der Test die Abschrift statt den Code.
        wd = (POLLERS / "data_watchdog.py").read_text()
        queries = re.findall(r'Source\(\s*"([^"]+)"', wd)
        if not queries:
            r.fail(name_q, "keine Queries in data_watchdog.py gefunden")
            return

        broken = []
        for q in queries:
            try:
                conn.execute(q)
            except Exception as e:
                broken.append(f"{q}\n  → {str(e).strip().splitlines()[0]}")
        if broken:
            r.fail(name_q, "\n".join(broken))
            return
        # Auf die Zahl der Erfolge festnageln, nicht auf die Abwesenheit eines
        # Fehlermusters: der erste Versuch dieses Tests war fälschlich grün,
        # weil psql seine Fehlerzeile mit 'psql:/dev/stdin:17:' einleitet und
        # ein '^ERROR'-Grep daran vorbeigriff.
        expected = len(_watchdog_sources())
        if len(queries) != expected:
            r.fail(name_q, f"{len(queries)} Queries extrahiert, aber "
                           f"{expected} Quellen in SOURCES — Extraktion unvollständig")
            return
        r.ok(f"{name_q} ({len(queries)} Queries)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Mount Midas Regression-Gate (MM-10)")
    ap.add_argument("--dsn", help="Postgres-DSN einer LEEREN Test-Datenbank")
    args = ap.parse_args()

    r = Result()

    print("--- Statisch ---")
    check_watchdog_completeness(r)
    check_poller_names_match_runner(r)
    check_no_unnamed_index_with_if_not_exists(r)
    check_rowcount_not_guessed(r)
    check_period_completeness_logic(r)

    print("\n--- Mit Datenbank ---")
    check_migrations_and_queries(r, args.dsn)

    print(f"\n=== {len(r.passed)} bestanden, {len(r.failures)} fehlgeschlagen, "
          f"{len(r.skipped)} übersprungen ===")
    if r.failures:
        for f in r.failures:
            print(f"  ❌ {f}")
        return 1
    if r.skipped:
        print("  ⚠️  Übersprungene Prüfungen sind NICHT bestanden — "
              "im PR-Gate läuft alles mit DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

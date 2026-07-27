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

def _frische_ausgenommen() -> set[str]:
    """
    Die dokumentierten Frische-Ausnahmen, gelesen aus data_watchdog.py selbst.

    Bewusst keine zweite Liste hier: eine Ausnahme, die an zwei Stellen gepflegt
    werden muss, driftet. Und jede Ausnahme dort MUSS eine Begründung tragen —
    das prüft check_watchdog_completeness mit.
    """
    text = (POLLERS / "data_watchdog.py").read_text()
    part = text.split("FRISCHE_AUSGENOMMEN: dict[str, str] = {", 1)
    if len(part) != 2:
        return set()
    body = part[1].split("\n}", 1)[0]
    return {m.group(1) for m in re.finditer(r'^\s*"([a-z_]+)":', body, re.M)}

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
    exempt = _frische_ausgenommen()

    if undeclared:
        r.fail(name, "Dynamischer INSERT (Tabellenname als Variable), aber nicht in "
                     "DYNAMIC_WRITES deklariert: " + ", ".join(undeclared))
        return

    missing = {t: sorted(v) for t, v in written.items()
               if t not in watched and t not in exempt}
    if missing:
        detail = "\n".join(f"{t} — geschrieben von {', '.join(v)}" for t, v in sorted(missing.items()))
        r.fail(name, f"Nicht in data_watchdog.SOURCES:\n{detail}")
        return

    # Gegenrichtung: eine bewachte Tabelle, die niemand schreibt, ist ein
    # Tippfehler oder toter Code — der Wächter wäre dort dauerhaft rot.
    orphan = sorted(watched - set(written) - exempt)
    if orphan:
        r.fail(name, "Bewacht, aber von keinem Poller beschrieben: " + ", ".join(orphan))
        return

    # Jede Ausnahme braucht eine Begruendung im Quelltext. Eine stumme Ausnahme
    # waere genau die Luecke, die diese Pruefung schliessen soll.
    wd = (POLLERS / "data_watchdog.py").read_text()
    for t in sorted(exempt):
        block = re.search(rf'"{t}":\s*\n?\s*"([^"]{{40,}})', wd, re.S)
        if not block:
            r.fail(name, f"Frische-Ausnahme '{t}' ohne verstaendliche Begruendung "
                         f"(mindestens ein Satz erwartet)")
            return
    suffix = f", {len(exempt)} begruendete Ausnahme(n)" if exempt else ""
    r.ok(f"{name} ({len(watched)} Quellen{suffix})")


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
    name = "Migrationen: wiederholbares DDL, kein nacktes DROP"
    unnamed, not_idem, naked_drop = [], [], []

    p_unnamed = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+ON\s", re.I)
    p_not_idem = re.compile(r"CREATE\s+(?:TABLE|(?:UNIQUE\s+)?INDEX)\s+(?!IF\s+NOT\s+EXISTS)", re.I)
    # Ein DROP am Zeilenanfang läuft unbedingt. Innerhalb eines DO-Blocks ist er
    # eingerückt und durch eine IF-Bedingung geschützt — das ist der zulässige Fall.
    p_naked_drop = re.compile(r"^DROP\s+(TABLE|INDEX)\s+(?!IF\s+EXISTS)", re.I)

    for sql in sorted(MIGRATIONS.glob("*.sql")):
        for i, line in enumerate(sql.read_text().splitlines(), 1):
            code = line.split("--", 1)[0]   # SQL-Kommentare ignorieren
            if p_unnamed.search(code):
                unnamed.append(f"{sql.name}:{i}: {line.strip()}")
            elif p_not_idem.search(code):
                not_idem.append(f"{sql.name}:{i}: {line.strip()}")
            if p_naked_drop.search(code):
                naked_drop.append(f"{sql.name}:{i}: {line.strip()}")

    problems = []
    if unnamed:
        problems.append("CREATE INDEX IF NOT EXISTS ohne Namen (Syntaxfehler):\n  "
                        + "\n  ".join(unnamed))
    if not_idem:
        problems.append("CREATE ohne IF NOT EXISTS (bricht beim zweiten Lauf ab bzw. "
                        "legt Duplikat-Indizes an):\n  " + "\n  ".join(not_idem))
    if naked_drop:
        problems.append("Unbedingtes DROP — scripts/migrate.sh läuft gegen die LIVE-DB, "
                        "das würde echte Daten löschen:\n  " + "\n  ".join(naked_drop))
    if problems:
        r.fail(name, "\n".join(problems))
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


def check_alert_cooldown_logic(r: Result) -> None:
    """
    Der Alarm-Cooldown muss am BEFUND-SET hängen, nicht an der einzelnen Quelle,
    und ein neuer Befund muss ihn sofort durchbrechen.

    Warum als Verhaltens-Test und nicht als Grep: die kaputte Fassung hatte
    ebenfalls einen Cooldown, ebenfalls 60 Minuten, und das Wort "gebündelt"
    stand sogar im Kommentar. Falsch war allein, WORAN er hing — pro Quelle
    statt am Set. Kein Schlüsselwort der Welt hätte das gefunden; sichtbar wird
    es erst, wenn zwei Quellen zu verschiedenen Zeiten stale werden.
    """
    name = "data_watchdog: Alarm-Cooldown (Verhalten, nicht Wortwahl)"
    text = (POLLERS / "data_watchdog.py").read_text()

    from datetime import datetime, timedelta, timezone
    ns: dict = {"datetime": datetime, "timezone": timezone,
                "_ALERT_COOLDOWN_MIN": 60}
    m = re.search(r"^def should_send\(.*?(?=\n\n\n|\n\nasync def |\n\ndef )",
                  text, re.S | re.M)
    if not m:
        r.fail(name, "should_send() nicht gefunden — muss eine reine, "
                     "testbare Funktion auf Modulebene sein")
        return
    try:
        exec(m.group(0), ns)
    except Exception as e:
        r.fail(name, f"should_send() nicht ausführbar: {e}")
        return
    send = ns["should_send"]

    U = timezone.utc
    T = datetime(2026, 7, 27, 12, 0, tzinfo=U)
    # (Befunde, letzte Signatur, zuletzt gesendet, erwartet, warum)
    cases = [
        (set(), None, None, False, "keine Befunde, nie gesendet"),
        (set(), "a", T - timedelta(minutes=1), False, "Befunde erholt — kein Entwarnungs-Spam"),
        ({"a"}, None, None, True, "erster Befund überhaupt"),
        ({"a"}, "a", T - timedelta(minutes=5), False, "gleiche Lage, Cooldown läuft"),
        ({"a"}, "a", T - timedelta(minutes=59), False, "gleiche Lage, kurz vor Ablauf"),
        ({"a"}, "a", T - timedelta(minutes=60), True, "gleiche Lage, Cooldown abgelaufen"),
        ({"a", "b"}, "a", T - timedelta(minutes=5), True,
         "b ist NEU kaputt — muss den Cooldown durchbrechen"),
        # Der eigentliche Defekt vom 27.07.: b wird stale, während a auf
        # Sperrfrist steht. Mit Cooldown pro Quelle kam b sofort als eigene
        # Nachricht und lief danach in seinem eigenen Stundenrhythmus weiter —
        # ab da dauerhaft Paare im Abstand von wenigen Minuten.
        ({"a", "b"}, "a,b", T - timedelta(minutes=5), False,
         "beide bereits gemeldet — genau EINE Nachricht pro Stunde, kein Paar"),
        ({"a"}, "a,b", T - timedelta(minutes=5), False,
         "Set geschrumpft, nichts Neues — schweigen"),
        ({"b", "a"}, "a,b", T - timedelta(minutes=5), False,
         "Reihenfolge darf nichts ändern"),
        ({"a"}, "a", None, True, "Signatur ohne Sendezeitpunkt — im Zweifel senden"),
    ]
    for befunde, sig, zuletzt, expected, why in cases:
        got = send(set(befunde), sig, zuletzt, T)
        if got != expected:
            r.fail(name, f"should_send({sorted(befunde)}, {sig!r}, {zuletzt}) = "
                         f"{got}, erwartet {expected}\nFall: {why}")
            return

    # Sabotage: hinge der Cooldown wieder an der einzelnen Quelle, müsste der
    # Paar-Fall oben senden. Er tut es nicht — sonst wäre dieser Test grün
    # geblieben, während das Verhalten kaputt ist.
    if send({"a", "b"}, "a,b", T - timedelta(minutes=5), T):
        r.fail(name, "Cooldown hängt nicht am Set")
        return

    r.ok(f"{name} ({len(cases)} Fälle)")


def check_runner_concurrency(r: Result) -> None:
    """
    Ein langsamer Poller darf die Schedule-Schleife nicht mehr aufhalten, und
    derselbe Poller darf nicht doppelt gleichzeitig laufen.

    Warum als Verhaltens-Test: die Wortwahl beweist hier gar nichts. `sleep(1)`
    im Quelltext zu finden sagt nichts darüber, ob der Job nebenläufig startet,
    und ein `threading.Thread(...)` im Text sagt nichts darüber, ob es auch
    gestartet wird. Geprüft wird deshalb die Zeit: der Wrapper muss sofort
    zurückkehren, obwohl der Poller lange läuft.

    Der Defekt, der hier eingefangen wird (gemessen am 27.07.): wallet_tracker
    lief im Schnitt 437s und blockierte dabei alles andere — zwischen zwei
    Läufen der 60s-Poller lagen bis zu 9.5 Minuten.
    """
    name = "runner: Poller blockieren einander nicht (Verhalten, nicht Wortwahl)"
    text = (POLLERS / "runner.py").read_text()

    import asyncio as _asyncio
    import threading as _threading
    import time as _time

    class _StummerLog:
        def __init__(self):
            self.warnungen = []

        def warning(self, msg, *args):
            self.warnungen.append(msg % args if args else msg)

        def error(self, msg, *args):
            pass

    gelaufen: list[str] = []

    async def _run_and_stamp(coro_fn, poller):
        gelaufen.append(poller)
        await coro_fn()

    stumm = _StummerLog()
    ns: dict = {
        "threading": _threading, "asyncio": _asyncio, "log": stumm,
        "_locks": {}, "_locks_guard": _threading.Lock(),
        "_run_and_stamp": _run_and_stamp,
    }
    for fname in ("_poller_name", "_lock_for", "is_overlapping", "execute", "run_async"):
        m = re.search(rf"^def {fname}\(.*?(?=\n\n\n|\n\nasync def |\n\ndef )",
                      text, re.S | re.M)
        if not m:
            r.fail(name, f"{fname}() nicht gefunden — muss eine Funktion auf "
                         f"Modulebene sein, damit dieser Test greifen kann")
            return
        try:
            exec(m.group(0), ns)
        except Exception as e:
            r.fail(name, f"{fname}() nicht ausführbar: {e}")
            return

    execute, run_async = ns["execute"], ns["run_async"]

    # 1. Der Wrapper muss sofort zurückkehren, obwohl der Poller 1.5s braucht.
    #    Mit dem alten seriellen Code hätte das hier 1.5s gedauert.
    langsam_laeuft = _threading.Event()

    async def _langsam():
        langsam_laeuft.set()
        await _asyncio.sleep(1.5)

    _langsam.__module__, _langsam.__qualname__ = "test", "langsam"
    t0 = _time.monotonic()
    run_async(_langsam)()
    dauer = _time.monotonic() - t0
    if dauer > 0.5:
        r.fail(name, f"run_async() blockierte {dauer:.2f}s — der geplante Lauf "
                     f"muss nebenläufig starten, sonst hält ein langsamer "
                     f"Poller die ganze Schleife auf")
        return

    # 2. Während der langsame Poller läuft, muss ein zweiter Takt desselben
    #    Pollers übersprungen werden — sonst stapelt er sich selbst auf.
    if not langsam_laeuft.wait(timeout=3):
        r.fail(name, "der nebenläufige Poller ist gar nicht angelaufen")
        return
    execute(_langsam, "test.langsam")
    if not any("übersprungen" in w for w in stumm.warnungen):
        r.fail(name, "ein bereits laufender Poller wurde NICHT übersprungen — "
                     "Überlappungssperre greift nicht")
        return
    if gelaufen.count("test.langsam") != 1:
        r.fail(name, f"test.langsam lief {gelaufen.count('test.langsam')}x "
                     f"gleichzeitig, erwartet genau 1x")
        return

    # 3. Ein ANDERER Poller darf davon nicht ausgesperrt werden — die Sperre
    #    gilt pro Poller, nicht global. Waere sie global, haetten wir den alten
    #    Defekt in neuer Form.
    async def _schnell():
        pass

    execute(_schnell, "test.schnell")
    if "test.schnell" not in gelaufen:
        r.fail(name, "ein anderer Poller wurde ausgesperrt — die Sperre muss "
                     "pro Poller gelten, nicht global")
        return

    # 4. Die Schleife darf nicht hinter der Arbeit eine volle Minute schlafen.
    #    Das ist der zweite Defekt: Periode = 60s PLUS Arbeitszeit, gemessen
    #    74s statt 60s bei den 60s-Pollern.
    m = re.search(r"while True:\s*\n\s*schedule\.run_pending\(\)\s*\n\s*time\.sleep\((\d+)\)",
                  text)
    if not m:
        r.fail(name, "Schedule-Schleife nicht in der erwarteten Form gefunden")
        return
    if int(m.group(1)) > 5:
        r.fail(name, f"Schedule-Schleife schläft {m.group(1)}s pro Runde — "
                     f"damit ist die Periode jedes Jobs 'Intervall PLUS "
                     f"Arbeitszeit'. Höchstens 5s.")
        return

    r.ok(f"{name} (nebenläufig in {dauer:.3f}s, Sperre pro Poller, "
         f"Schleifentakt {m.group(1)}s)")


def check_panels_no_invented_values(r: Result) -> None:
    """
    Kein Panel darf einen Wert erfinden, wenn die Daten fehlen.

    Zwei mechanisch prüfbare Formen:

    1. Ein Einzelwert-Panel (stat/gauge) mit 'ORDER BY ... DESC LIMIT 1' ohne
       Zeitgrenze zeigt den letzten Wert ewig weiter. Stirbt der Poller, friert
       die Kachel ein und sieht weiter lebendig aus.
    2. COALESCE(..., <Literal ungleich 0>) setzt einen frei gewählten Ersatzwert
       ein. Die Volume-Profile-Kachel hatte so VAH/POC/VAL fest mit 2.90/2.40/2.27
       hinterlegt — rund ein Drittel über den echten Werten.

    COALESCE(..., 0) ist ausdrücklich erlaubt: bei einer Aggregation über ein
    Zeitfenster ist "nichts passiert" eine echte Null.
    """
    name = "Panels erfinden keine Werte (Einzelwert ohne Zeitgrenze / Ersatz-Literal)"
    dash_dir = REPO / "grafana" / "dashboards"
    if not dash_dir.is_dir():
        r.fail(name, "grafana/dashboards nicht gefunden")
        return

    import json

    unbounded: list[str] = []
    invented: list[str] = []
    time_bound = re.compile(r"NOW\(\)\s*-\s*INTERVAL|CURRENT_DATE|\$__time", re.I)
    latest_one = re.compile(r"ORDER BY\s+\w+\s+DESC\s+LIMIT\s+1", re.I)
    # COALESCE(..., <zahl>) mit einer Zahl, die nicht 0 / 0.0 ist
    fake_default = re.compile(r"COALESCE\s*\((?:[^()]|\([^()]*\))*,\s*(\d+\.\d+|[1-9]\d*)\s*\)", re.I)

    for path in sorted(dash_dir.glob("*.json")):
        try:
            dash = json.loads(path.read_text())
        except Exception as e:
            r.fail(name, f"{path.name}: nicht lesbar ({e})")
            return
        for p in dash.get("panels", []):
            for t in p.get("targets", []):
                sql = " ".join((t.get("rawSql") or "").split())
                if not sql:
                    continue
                where = f'{path.name} · {p.get("id")} "{p.get("title", "")[:38]}"'
                if (p.get("type") in ("stat", "gauge")
                        and latest_one.search(sql) and not time_bound.search(sql)):
                    unbounded.append(where)
                for m in fake_default.finditer(sql):
                    invented.append(f"{where} — Ersatzwert {m.group(1)}")

    problems = []
    if unbounded:
        problems.append("Einzelwert ohne Zeitgrenze:\n  " + "\n  ".join(unbounded))
    if invented:
        problems.append("Erfundener Ersatzwert:\n  " + "\n  ".join(invented))
    if problems:
        r.fail(name, "\n".join(problems))
        return
    r.ok(name)


# ── Prüfungen mit Datenbank ───────────────────────────────────────────────────

def check_migrations_and_queries(r: Result, dsn: str | None) -> None:
    name_mig  = "Migrationen laufen auf leerer DB durch"
    name_idem = "Migrationen sind wiederholbar (Doppellauf, keine Duplikat-Indizes)"
    name_q    = "Wächter-Queries laufen gegen das Migrations-Schema"

    if not dsn:
        for n in (name_mig, name_idem, name_q):
            r.skip(n, "kein --dsn angegeben")
        return

    try:
        import psycopg
    except ImportError:
        for n in (name_mig, name_idem, name_q):
            r.skip(n, "psycopg nicht installiert")
        return

    files = sorted(MIGRATIONS.glob("*.sql"))
    with psycopg.connect(dsn, autocommit=True) as conn:
        for sql in files:
            try:
                conn.execute(sql.read_text())
            except Exception as e:
                r.fail(name_mig, f"{sql.name}: {e}")
                for n in (name_idem, name_q):
                    r.skip(n, "Migrationen fehlgeschlagen")
                return
        r.ok(f"{name_mig} ({len(files)} Dateien)")

        # ── Doppellauf ───────────────────────────────────────────────────────
        # scripts/migrate.sh lässt ALLE Migrationen gegen die laufende DB
        # durchlaufen. Das setzt Wiederholbarkeit voraus — und die war nicht
        # gegeben: CREATE TABLE ohne IF NOT EXISTS brach ab, namenlose
        # CREATE INDEX legten still Duplikate an, und Migration 003 hätte mit
        # einem nackten DROP TABLE echte Daten gelöscht.
        def snapshot() -> tuple[set, set, dict]:
            tabs = {r0[0] for r0 in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
            idx = {r0[0] for r0 in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public'").fetchall()}
            # Zeilenzahlen mit: ein reiner Schema-Vergleich übersieht Seed-Daten,
            # die sich vervielfachen. np_remuneration wuchs so auf 27 statt 9
            # Zeilen — sein "ON CONFLICT DO NOTHING" hatte kein Konfliktziel und
            # griff nie. Der erste Entwurf dieser Prüfung hat das durchgelassen.
            counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                      for t in sorted(tabs)}
            return tabs, idx, counts

        tabs_before, idx_before, counts_before = snapshot()

        for sql in files:
            try:
                conn.execute(sql.read_text())
            except Exception as e:
                r.fail(name_idem, f"{sql.name} beim zweiten Lauf: {e}")
                r.skip(name_q, "Doppellauf fehlgeschlagen")
                return

        tabs_after, idx_after, counts_after = snapshot()
        problems = []
        if tabs_after != tabs_before:
            problems.append(f"Tabellen verändert: "
                            f"+{sorted(tabs_after - tabs_before)} "
                            f"-{sorted(tabs_before - tabs_after)}")
        if idx_after != idx_before:
            problems.append(f"Indizes verändert (Duplikate?): "
                            f"+{sorted(idx_after - idx_before)} "
                            f"-{sorted(idx_before - idx_after)}")
        grown = {t: (counts_before.get(t), counts_after[t])
                 for t in counts_after
                 if counts_before.get(t) != counts_after[t]}
        if grown:
            problems.append("Seed-Daten vervielfacht:\n  " + "\n  ".join(
                f"{t}: {a} → {b}" for t, (a, b) in sorted(grown.items())))
        if problems:
            r.fail(name_idem, "\n".join(problems))
            return
        seeded = sum(1 for v in counts_after.values() if v)
        r.ok(f"{name_idem} — {len(tabs_after)} Tabellen, {len(idx_after)} Indizes, "
             f"{seeded} Seed-Tabellen stabil")

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
    check_alert_cooldown_logic(r)
    check_runner_concurrency(r)
    check_panels_no_invented_values(r)

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

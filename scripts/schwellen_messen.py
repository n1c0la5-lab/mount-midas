#!/usr/bin/env python3
"""
schwellen_messen.py — Wächter-Schwellen aus Messung herleiten (MM-10)

Beantwortet EINE Frage pro Quelle: wie alt wird sie im gesunden Betrieb
wirklich? Daraus ein Schwellen-Vorschlag — mehr nicht. Das Skript schreibt
nichts und ändert nichts; die Entscheidung trifft ein Mensch.

  python3 scripts/schwellen_messen.py --dsn "..." [--seit ISO] [--faktor 2.0]

── Warum es das gibt ─────────────────────────────────────────────────────────
Die Schwellen der Schnell-Schicht standen viel zu weit: ob_snapshots und
signal_log auf 20 Minuten bei gemessenen max 11 Minuten. Diese 11 Minuten waren
aber kein Marktbefund, sondern der einsträngige runner — wallet_tracker
blockierte ihn stündlich 7,3 Minuten (behoben 2026-07-27, PR #18). Die
Schwellen waren also Notwehr gegen einen Nebenläufigkeitsfehler und haben die
Schnell-Schicht blind gemacht.

Nach so einem Eingriff MUSS neu gemessen werden — sonst bleibt die Notwehr
stehen, obwohl der Angreifer weg ist. Und sie muss reproduzierbar sein, nicht
aus zusammengeklickter SQL bestehen.

── Welche Größe gemessen wird ────────────────────────────────────────────────
`now() - max(ts)` der Zieltabelle, wie system_health sie protokolliert — also
GENAU die Größe, die der Wächter prüft. Nicht der Abstand zwischen Läufen in
poller_runs und nicht der Abstand zwischen Zeilen: bei den Trade-Tabellen sind
das drei verschiedene Dinge. Pro Abruf kommen viele Zeilen mit Börsen-
Zeitstempeln herein, die Zeilen-Abstände bleiben also klein, während max(ts)
zurückfallen kann. Wer die falsche Größe misst, setzt eine Schwelle, die mit
dem Alarm nichts zu tun hat.

── Was das Skript NICHT tut ──────────────────────────────────────────────────
* Es schlägt für Event-Quellen nichts vor. Deren Weite ist Marktinformation:
  ICP hat über lange Strecken keine Liquidationen, das ist kein Defekt.
* Es schlägt bei zu wenig Messungen nichts vor. Eine Schwelle aus 14 Messungen
  ist geraten, nicht gemessen — und NULL ist nicht neutral.
* Es schlägt nichts vor, wenn das Fenster einen echten Ausfall enthält. Dann
  ist die Messung kontaminiert; ausgeschlossen wird über Kontamination, nie
  über ein unbequemes Ergebnis.
* Es verengt nie unter das, was es gesehen hat. Eine zu enge Schwelle ist kein
  sicherer Fehler: 34% Fehlalarmquote haben am 2026-07-04 dazu geführt, dass
  der Kanal stummgeschaltet wurde — danach blieb ein echter 31-Stunden-Ausfall
  unsichtbar.

── Woher der Faktor kommt ────────────────────────────────────────────────────
Aus den bereits gesetzten Schwellen, nicht aus der Luft. Die im Wächter
dokumentierten Paare (gemessenes Max -> gesetzte Schwelle) ergeben:

    liquidation_events   31.5h -> 48h    1.52x
    open_interest        71.4min -> 120min  1.68x
    funding_rates        71.3min -> 120min  1.68x
    ob_snapshots         11.0min -> 20min   1.82x
    volume_profile       40.6min -> 100min  2.46x
    spot_trades           2.7min -> 10min   3.70x

Der Median liegt bei rund 1.8x, die kleinen Absolutwerte bekamen mehr Luft.
Default ist deshalb 2.0x, plus mindestens 5 Minuten Kopffreiheit — bei einer
Quelle mit 2 Minuten Max wären 2x sonst 4 Minuten, und der erste Netz-Schluckauf
löst Alarm aus. Der Faktor ist als Flag änderbar; er ist eine Konvention, keine
Messung, und wird deshalb im Bericht ausgewiesen.
"""
import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

# Ab hier ist der runner nebenläufig (PR #18). Alles davor ist durch den
# blockierenden wallet_tracker verfälscht und darf nicht in eine Schwelle
# eingehen — deshalb warnt das Skript, statt still Müll zu verrechnen.
RUNNER_CUTOVER = datetime(2026, 7, 27, 9, 6, 31, tzinfo=timezone.utc)

MIN_MESSUNGEN = 100
MIN_KOPFFREIHEIT_MIN = 5.0


def _lade_sources() -> dict:
    """
    data_watchdog.SOURCES importieren statt parsen.

    Das Skript läuft im Poller-Container (siehe schwellen_messen.sh), dort sind
    die DB-Umgebungsvariablen gesetzt, die data_watchdog beim Import braucht.
    Ein Regex-Parser wäre stiller Betrug: er würde bei jeder Umformatierung
    lautlos die halbe Liste verlieren und trotzdem eine Tabelle ausgeben.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pollers"))
    try:
        import data_watchdog
    except KeyError as e:
        sys.exit(f"FEHLER: Umgebungsvariable {e} fehlt. Das Skript gehört in den "
                 f"Poller-Container — nimm scripts/schwellen_messen.sh.")
    return data_watchdog.SOURCES


def _runden(minuten: float) -> int:
    """Auf eine Zahl runden, die ein Mensch in den Quelltext schreiben würde."""
    if minuten <= 60:
        return int(math.ceil(minuten / 5.0) * 5)
    if minuten <= 360:
        return int(math.ceil(minuten / 10.0) * 10)
    return int(math.ceil(minuten / 60.0) * 60)


def _als_quelltext(minuten: int) -> str:
    """Im Idiom des Wächters ausdrücken: 120 -> '2 * HOUR'."""
    if minuten % 1440 == 0:
        return f"{minuten // 1440} * DAY"
    if minuten % 60 == 0:
        return f"{minuten // 60} * HOUR"
    return f"{minuten} * MIN"


def _fmt(minuten) -> str:
    if minuten is None:
        return "—"
    if minuten < 90:
        return f"{minuten:.1f}min"
    if minuten < 2880:
        return f"{minuten / 60:.1f}h"
    return f"{minuten / 1440:.1f}d"


def messen(conn, quellen: list[str], seit: datetime) -> dict:
    rows = conn.execute(
        """
        SELECT source,
               count(*)                                                    AS n,
               max(age_minutes)                                            AS max_min,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY age_minutes)   AS p95,
               percentile_cont(0.99) WITHIN GROUP (ORDER BY age_minutes)   AS p99,
               count(*) FILTER (WHERE is_stale)                            AS stale_n,
               count(*) FILTER (WHERE age_minutes IS NULL)                 AS ohne_daten
        FROM system_health
        WHERE ts >= %s AND source = ANY(%s)
        GROUP BY source
        """,
        (seit, quellen),
    ).fetchall()
    return {r[0]: {"n": r[1], "max": r[2], "p95": r[3], "p99": r[4],
                   "stale_n": r[5], "ohne_daten": r[6]} for r in rows}


def bewerten(name: str, src, m: dict | None, faktor: float) -> tuple[str, int | None, str]:
    """
    Reine Entscheidung: Vorschlag, oder begründete Enthaltung?

    Gibt (status, vorschlag_min, begruendung) zurück. status ist einer von
    "vorschlag", "unveraendert", "enthaltung".
    """
    if src.kind == "event":
        return ("enthaltung", None,
                "Event-Quelle — Alter ist Marktinformation, nicht Takt. "
                "Schicht A klärt, ob der Poller läuft.")
    if m is None or m["n"] == 0:
        return ("enthaltung", None, "keine Messungen im Fenster")
    if m["n"] < MIN_MESSUNGEN:
        return ("enthaltung", None,
                f"nur {m['n']} Messungen (mindestens {MIN_MESSUNGEN}) — "
                f"das wäre geraten, nicht gemessen")
    if m["ohne_daten"]:
        return ("enthaltung", None,
                f"{m['ohne_daten']} Messungen ohne Daten (Tabelle leer) — "
                f"Fenster kontaminiert")
    if m["stale_n"]:
        return ("enthaltung", None,
                f"{m['stale_n']} von {m['n']} Messungen waren stale — das "
                f"Fenster enthält einen echten Ausfall, Messung kontaminiert")

    roh = max(float(m["max"]) * faktor, float(m["max"]) + MIN_KOPFFREIHEIT_MIN)
    vorschlag = _runden(roh)
    if vorschlag >= src.threshold_min:
        return ("unveraendert", src.threshold_min,
                f"Vorschlag {vorschlag}min läge nicht unter der aktuellen "
                f"Schwelle — keine Verengung vertretbar")
    return ("vorschlag", vorschlag, "")


def main() -> int:
    ap = argparse.ArgumentParser(description="Wächter-Schwellen aus system_health messen")
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--seit", help="ISO-Zeitstempel; Default: 24h zurück")
    ap.add_argument("--faktor", type=float, default=2.0,
                    help="Sicherheitsfaktor auf das gemessene Maximum (Default 2.0)")
    args = ap.parse_args()

    seit = (datetime.fromisoformat(args.seit) if args.seit
            else datetime.now(timezone.utc) - timedelta(hours=24))
    if seit.tzinfo is None:
        seit = seit.replace(tzinfo=timezone.utc)

    SOURCES = _lade_sources()
    jetzt = datetime.now(timezone.utc)
    fenster_h = (jetzt - seit).total_seconds() / 3600

    print(f"=== Schwellen-Messung — Fenster {seit:%Y-%m-%d %H:%M} → jetzt "
          f"({fenster_h:.1f}h), Faktor {args.faktor}x ===\n")

    if seit < RUNNER_CUTOVER:
        print(f"⚠️  WARNUNG: Das Fenster beginnt VOR dem runner-Cutover "
              f"({RUNNER_CUTOVER:%Y-%m-%d %H:%M}).")
        print("    Davor blockierte wallet_tracker den einsträngigen runner "
              "stündlich ~7 min.")
        print("    Die Messung ist dadurch nach oben verfälscht und taugt "
              "nicht als Schwelle.\n")

    with psycopg.connect(args.dsn) as conn:
        gemessen = messen(conn, list(SOURCES), seit)

    kopf = (f"{'Quelle':<28}{'n':>6}{'max':>10}{'p95':>10}{'p99':>10}"
            f"{'aktuell':>10}{'Vorschlag':>12}")
    print(kopf)
    print("-" * len(kopf))

    vorschlaege: list[tuple[str, int]] = []
    enthaltungen: list[tuple[str, str]] = []

    for name, src in sorted(SOURCES.items()):
        m = gemessen.get(name)
        status, vorschlag, grund = bewerten(name, src, m, args.faktor)
        n = m["n"] if m else 0
        zeile = (f"{name:<28}{n:>6}"
                 f"{_fmt(float(m['max']) if m and m['max'] is not None else None):>10}"
                 f"{_fmt(float(m['p95']) if m and m['p95'] is not None else None):>10}"
                 f"{_fmt(float(m['p99']) if m and m['p99'] is not None else None):>10}"
                 f"{_fmt(src.threshold_min):>10}")
        if status == "vorschlag":
            spar = 100 * (1 - vorschlag / src.threshold_min)
            print(f"{zeile}{_fmt(vorschlag) + f' (-{spar:.0f}%)':>12}")
            vorschlaege.append((name, vorschlag))
        else:
            print(f"{zeile}{'—':>12}")
            enthaltungen.append((name, grund))

    if enthaltungen:
        print("\n--- Keine Empfehlung (mit Begründung) ---")
        for name, grund in enthaltungen:
            print(f"  {name}: {grund}")

    if vorschlaege:
        print("\n--- Vorschlag als Quelltext (data_watchdog.SOURCES) ---")
        print("    Nicht automatisch übernehmen: jede Zeile braucht den "
              "Kommentar, WORAUS sie stammt.")
        for name, minuten in vorschlaege:
            print(f"    {name:<28} {_als_quelltext(minuten):>12}   "
                  f"# gemessen max {_fmt(float(gemessen[name]['max']))}, "
                  f"n={gemessen[name]['n']}, Fenster {fenster_h:.0f}h, "
                  f"Faktor {args.faktor}x")
    else:
        print("\nKein Vorschlag — nichts zu verengen oder Datenlage zu dünn.")

    print(f"\n=== {len(vorschlaege)} Vorschläge, {len(enthaltungen)} Enthaltungen ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

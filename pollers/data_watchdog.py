"""
data_watchdog.py — Datenqualitäts-Wächter (MM-10)

Prüft alle 5 Minuten zwei getrennte Fragen und verrechnet sie nie:

  Schicht A — Lauf-Stempel (poller_runs): LIEF der Poller?
  Schicht B — Daten-Frische (max(ts)):    KOMMEN Daten an?

Warum getrennt: max(ts) allein kann "Poller kaputt" nicht von "Markt still"
unterscheiden. liquidation_events darf legitim acht Stunden leer bleiben,
wallet_movements einen Tag. Umgekehrt lief dre_metrics zwölf Tage lang brav und
schrieb stillschweigend nichts — kein Frische-Check der Welt hätte das gefunden,
weil der Poller ja lief. Erst beide Schichten zusammen ergeben eine Aussage.

Ergebnisse landen in system_health, Alarme gehen gebündelt per Telegram raus:
EIN Alarm für alle aktuellen Befunde, höchstens 1x/Stunde — der Cooldown hängt am
Befund-Set und liegt in der DB, überlebt also Deploys. Neu auftretende Befunde
durchbrechen den Cooldown sofort.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
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

# Das system_health-Schema gehört Migration 010, nicht dieser Datei. Das frühere
# CREATE TABLE/INDEX IF NOT EXISTS im Lauf-Pfad ist raus: seit der runner
# nebenläufig ist, erzeugt DDL im heissen Pfad Deadlocks gegen die eigenen
# INSERTs (ShareLock gegen RowExclusiveLock) — siehe run_stamp.py. Dass das
# Schema da ist, sichern migrate.sh und die Schema-Drift-Prüfung in gate.sh.

MIN  = 1
HOUR = 60
DAY  = 24 * 60


@dataclass(frozen=True)
class Source:
    """
    Eine überwachte Datenquelle.

    kind="takt":  wird bei jedem Poller-Lauf beschrieben. Schwelle ≈ 3× Takt,
                  damit ein einzelner verpasster Lauf nicht sofort schreit.
    kind="event": wird nur bei Marktereignissen beschrieben. Alter allein ist
                  kein Defekt — die weite Schwelle fängt nur den Totalausfall,
                  die eigentliche Wache läuft über Schicht A.
    """
    query: str
    threshold_min: int
    kind: str


# ── Schicht B: Daten-Frische ──────────────────────────────────────────────────
#
# Vollständigkeits-Regel: JEDE poller-geschriebene Tabelle steht hier. Vorher
# waren es 7 von 25 — und zwar ausgerechnet die laute Schnell-Schicht, während
# fünf stille Tabellen zwischen 33 Stunden und 12 Tagen unbemerkt standen.
# key_levels und np_remuneration (manuell/statisch gepflegt) sind ausgenommen.
#
# ── Woher die Schwellen kommen ────────────────────────────────────────────────
# Nicht geschätzt, sondern aus 14 Tagen system_health gemessen — also aus genau
# der Größe, die hier geprüft wird (now() - max(ts)), nicht aus dem Abstand
# zwischen Zeilen. Bei Trade-Tabellen sind das zwei verschiedene Dinge: pro
# Abruf kommen viele Zeilen mit Börsen-Zeitstempeln, die Abstände bleiben also
# klein, während max(ts) zurückfallen kann.
#
#   Quelle              alte Schw.  max gemessen   Alarmquote      neu
#   liquidation_events      480min      1894min       34.38%      48h (Event)
#   ob_snapshots              5min        11.0min      2.94%      20min
#   signal_log                5min        10.9min      2.91%      20min
#   volume_profile         1560min        40.6min      0%        100min
#   open_interest            90min        71.4min      0%        120min
#   funding_rates            90min        71.3min      0%        120min
#   spot_trades               5min         2.7min      0%         10min
#
# Diese drei Quellen erzeugten zusammen rund 40% Alarmquote — bei Prüfung alle
# 5 Minuten und 60-Minuten-Cooldown bis zu 72 Telegramme am Tag, praktisch alle
# falsch. Deshalb wurde der Alarmkanal am 2026-07-04 stummgeschaltet, und
# deshalb blieb danach auch ein echter 31-Stunden-Ausfall unsichtbar.
# Eine zu enge Schwelle ist kein sicherer Fehler: sie kostet den ganzen Kanal.
#
# Wo keine Messung vorliegt, steht die Begründung dabei. Geraten wird nicht.
SOURCES: dict[str, Source] = {
    # 60s-Takt. Die 8–12min-Ausschläge sind echt und haben eine Ursache: der
    # runner ist einsträngig, und wallet_tracker blockiert ihn stündlich rund
    # 7 Minuten (siehe MM-10 "Offene Punkte").
    "spot_trades":        Source("SELECT MAX(ts) FROM spot_trades",             10 * MIN,  "takt"),
    # perp_trades: keine system_health-Historie (war nie bewacht). Gleicher
    # Poller-Pfad wie spot_trades, aber OHNE den WS-Collector als zweite
    # Quelle — deshalb dieselbe Schwelle wie die anderen 60s-Tabellen, nicht
    # die von spot_trades.
    "perp_trades":        Source("SELECT MAX(ts) FROM perp_trades",             20 * MIN,  "takt"),
    "ob_snapshots":       Source("SELECT MAX(ts) FROM ob_snapshots",            20 * MIN,  "takt"),
    "signal_log":         Source("SELECT MAX(ts) FROM signal_log",              20 * MIN,  "takt"),
    "market_activity":    Source("SELECT MAX(ts) FROM market_activity",         20 * MIN,  "takt"),
    # 5min-Takt (gemessener Abstand max 16.4min — der blockierende runner wirkt
    # auch hier)
    "system_health":      Source("SELECT MAX(ts) FROM system_health",           30 * MIN,  "takt"),
    # Schicht A muss selbst bewacht sein — verstummt poller_runs, ist die
    # Lauf-Überwachung blind, ohne dass es auffällt.
    "poller_runs":        Source("SELECT MAX(started_at) FROM poller_runs",     30 * MIN,  "takt"),
    "master_agent_log":   Source("SELECT MAX(ts) FROM master_agent_log",        30 * MIN,  "takt"),
    # 15min-Takt (gemessener Abstand max 25.3min)
    "epz_scores":         Source("SELECT MAX(ts) FROM epz_scores",              50 * MIN,  "takt"),
    "summary_stats":      Source("SELECT MAX(calculated_at) FROM summary_stats", 50 * MIN,  "takt"),
    # liquidation_snapshots: max 36.9min gemessen, deshalb weiter als die
    # anderen beiden 15min-Tabellen
    "liquidation_snapshots": Source("SELECT MAX(ts) FROM liquidation_snapshots", 60 * MIN,  "takt"),
    # 30min-Takt (max 40.6min)
    "volume_profile":     Source("SELECT MAX(calculated_at) FROM volume_profile", 100 * MIN, "takt"),
    # Stündlich (max 71.4min)
    "funding_rates":      Source("SELECT MAX(ts) FROM funding_rates",            2 * HOUR, "takt"),
    "open_interest":      Source("SELECT MAX(ts) FROM open_interest",            2 * HOUR, "takt"),
    "neuron_dissolve_snapshots": Source(
        "SELECT MAX(ts) FROM neuron_dissolve_snapshots",                         2 * HOUR, "takt"),
    # Täglich
    # ohlcv_daily: `date` ist ein TAGESDATUM, kein Schreibzeitpunkt. Der Lauf um
    # 00:05 schreibt die Kerze des VORTAGS mit date = Vortag 00:00. Damit ist
    # max(date) strukturell zwischen 24h und 48h "alt", ohne dass etwas fehlt —
    # beim ersten scharfen Lauf schlug die 30h-Schwelle bei 35.7h prompt an.
    # 60h = zwei verpasste Läufe plus Puffer, gemessen an der Struktur der Spalte.
    "ohlcv_daily":        Source("SELECT MAX(date)::timestamptz FROM ohlcv_daily", 60 * HOUR, "takt"),
    # updated_at, NICHT last_mint_at: np_poller setzt updated_at bei jedem
    # Upsert, last_mint_at ist bei allen 104 Zeilen NULL (tote Spalte, MM-10
    # "Offene Punkte"). Ein Frische-Check darauf wäre dauerhaft rot.
    "np_providers":       Source("SELECT MAX(updated_at) FROM np_providers",     30 * HOUR, "takt"),
    "np_reward_mints":    Source("SELECT MAX(fetched_at) FROM np_reward_mints",  30 * HOUR, "takt"),
    "np_threshold_daily": Source("SELECT MAX(ts) FROM np_threshold_daily",       30 * HOUR, "takt"),
    "threshold_aggregate_daily": Source(
        "SELECT MAX(ts) FROM threshold_aggregate_daily",                         30 * HOUR, "takt"),
    "xdr_rates":          Source("SELECT MAX(ts) FROM xdr_rates",                30 * HOUR, "takt"),
    # np_performance: fetched_at, NICHT ts. ts ist der Metrik-TAG, und DRE-
    # Belohnungsperioden laufen vom 14. zum 14., nicht über Kalendermonate — der
    # Ausgabeordner heisst "rewards_2026-06-14_to_2026-07-14". Der neueste
    # verfügbare Tag bleibt deshalb strukturell bis zu einem Monat stehen, bis
    # die nächste Periode schliesst, ohne dass irgendetwas fehlt.
    # Die alte Schwelle von 72h auf MAX(ts) war ausdrücklich als "begründete
    # Obergrenze, KEIN gemessener Wert" markiert. Sie lag um Faktor 10 daneben
    # und meldete am 27.07. eine kerngesunde Tabelle 13 Tage lang rund um die
    # Uhr als stale — 24 Fehlalarme am Tag. Genau das Muster, das den Kanal
    # schon einmal gekostet hat.
    # Auf fetched_at ist die Frage wieder täglich beantwortbar (30h wie die
    # anderen Tages-Poller), weil dre_metrics jede Nacht die laufende Periode neu
    # stempelt — auch wenn kein neuer Metrik-Tag dazukommt.
    "np_performance":     Source("SELECT MAX(fetched_at) FROM np_performance",   30 * HOUR, "takt"),

    # Event-getrieben — Alter allein ist hier kein Defekt.
    #
    # liquidation_events war mit 8h die lauteste Fehlerquelle im ganzen System:
    # 34.38% Alarmquote, gemessenes Alter im Schnitt 439min, maximal 1894min
    # (31.5h). ICP hat einfach über lange Strecken keine Liquidationen — das ist
    # Marktinformation, kein Defekt. Ob okx_liq_poller läuft, klärt Schicht A.
    "liquidation_events": Source("SELECT MAX(ts) FROM liquidation_events",       48 * HOUR, "event"),
    # wallet_movements: 48h war ebenfalls zu eng — 31.16% Alarmquote über 199
    # Messungen, praktisch dieselbe Größenordnung wie die 34% von
    # liquidation_events. Gemessen am 27.07. aus zwei Richtungen:
    #   system_health-Historie:            max 54.2h, Schnitt 44.2h
    #   echte Lücken zwischen Bewegungen:  max 69.0h, Schnitt 1.2h (60 Tage)
    # Maßgeblich ist die Lücken-Messung: bei einer Event-Tabelle ist der Abstand
    # zwischen zwei Bewegungen genau die Größe, die hier gefragt ist. 120h = die
    # gemessenen 69h plus reichlich Puffer. Ob wallet_tracker läuft, klärt
    # Schicht A — dafür braucht es keine enge Frische-Schwelle.
    "wallet_movements":   Source("SELECT MAX(ts) FROM wallet_movements",        120 * HOUR, "event"),
    "destination_clusters": Source(
        "SELECT MAX(first_seen_at) FROM destination_clusters",                   14 * DAY,  "event"),
    # np_wallet_labels bewusst NICHT hier: siehe FRISCHE_AUSGENOMMEN.
}

# Poller-geschriebene Tabellen ohne Frische-Prüfung — mit Begründung, denn eine
# stumme Ausnahme ist genau die Lücke, die MM-10 geschlossen hat. Ihre Poller
# stehen weiter unter der Lauf-Wache (Schicht A).
FRISCHE_AUSGENOMMEN: dict[str, str] = {
    "np_wallet_labels":
        "Manuell gepflegte Exchange-Labels. seed_wallet_labels() legt nur NEUE "
        "Labels an, added_at bleibt sonst stehen — 63 Tage ohne neues Label sind "
        "der Normalfall, kein Defekt. Beim ersten scharfen Lauf schlug eine "
        "30-Tage-Schwelle hier sofort falsch an.",
    "watchdog_alert_state":
        "Cooldown-Zustand des Wächters, geschrieben NUR wenn ein Alarm rausgeht. "
        "Der Normalfall ist, dass nichts kaputt ist — die Zeile bleibt dann "
        "beliebig lange stehen. Eine Frische-Schwelle darauf würde Schweigen als "
        "Defekt lesen und hätte genau die Verhaltensweise, die MM-10 abstellt: "
        "sie meldete umso lauter, je gesünder das System ist.",
}

# ── Schicht A: Lauf-Stempel ───────────────────────────────────────────────────
#
# Poller-Name (wie runner.py ihn stempelt) → maximale erlaubte Pause zwischen
# zwei Läufen. Schwelle ≈ 3× Takt, gleiche Logik wie oben.
#
# hl_ws_collector fehlt bewusst: er läuft als eigener Container, nicht über den
# runner, und wird daher nicht gestempelt. Seine Gesundheit hängt an
# spot_trades (5min) in Schicht B.
POLLER_CADENCE: dict[str, int] = {
    "ob_poller.run":                     5 * MIN,
    "tick_collector.run":                5 * MIN,
    "signal_engine.run":                 5 * MIN,
    "data_watchdog.run":                20 * MIN,
    "master_agent.run":                 20 * MIN,
    "okx_liq_poller.run":               20 * MIN,
    "liq_poller.run":                   50 * MIN,
    "epz_calculator.run":               50 * MIN,
    "correlation_calculator.run":       50 * MIN,
    "volume_profile_calculator.run":   100 * MIN,
    "wallet_tracker.run":                3 * HOUR,
    "neuron_poller.run":                 3 * HOUR,
    "tick_collector.run_market_data":    3 * HOUR,
    "np_poller.run":                    30 * HOUR,
    "dre_metrics.run":                  30 * HOUR,
    "threshold_calculator.run":         30 * HOUR,
    "tick_collector.run_ohlcv":         30 * HOUR,
    "tick_collector.run_cleanup":       30 * HOUR,
}

_LAUF_PREFIX  = "lauf:"
_KANAL_SOURCE = "kanal:telegram"

_ALERT_COOLDOWN_MIN = 60


async def _send_telegram(session: aiohttp.ClientSession, text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("data_watchdog: kein Telegram-Token/Chat — Alarm bleibt stumm")
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


def _signature(namen: list[str]) -> str:
    """Kanonische Kennung eines Befund-Sets — Reihenfolge darf nichts ändern."""
    return ",".join(sorted(set(namen)))


def should_send(
    befunde: set[str],
    letzte_signatur: str | None,
    zuletzt_gesendet: datetime | None,
    now: datetime,
    cooldown_min: int = _ALERT_COOLDOWN_MIN,
) -> bool:
    """
    Reine Entscheidung: geht jetzt ein Alarm raus?

    Bewusst ohne DB-Zugriff, damit das Gate echtes Verhalten prüfen kann und
    nicht nur, ob die richtigen Wörter im Quelltext stehen.

    Der Cooldown hängt am BEFUND-SET, nicht an der einzelnen Quelle. Vorher hing
    er pro Quelle — und das hat die Bündelung ausgehebelt, sobald zwei Quellen zu
    verschiedenen Zeiten stale wurden: jede lief in ihrem eigenen Stundenrhythmus
    weiter. Am 27.07. kamen so 26 Nachrichten in 20 Stunden, ab 01:23 durchgehend
    paarweise im Abstand von 5,5 Minuten. Bei N stale Quellen wären es N
    Nachrichten pro Stunde gewesen statt der einen, die gemeint war.

    Drei Regeln:

    1. Keine Befunde → nichts senden. Schweigen ist die Normallage.
    2. Etwas NEUES ist kaputt → sofort senden, egal wie frisch der letzte Alarm
       ist. Ein echter neuer Ausfall darf nicht bis zu 60 Minuten warten, nur
       weil eine andere Quelle gerade gemeldet hat.
    3. Sonst → erst nach Ablauf des Cooldowns, als Erinnerung an eine bereits
       gemeldete Lage.

    Ein SCHRUMPFENDES Set löst absichtlich nichts aus: eine Quelle, die sich
    erholt, ist keine Nachricht wert, und eine flatternde Quelle würde sonst bei
    jedem Wechsel senden. Der gespeicherte Zustand wird nur beim tatsächlichen
    Senden fortgeschrieben (siehe run()) — dadurch gilt eine Quelle, die still
    verschwindet und wiederkommt, erst nach dem nächsten regulären Alarm wieder
    als neu.
    """
    if not befunde:
        return False
    if zuletzt_gesendet is None or letzte_signatur is None:
        return True
    bekannt = set(letzte_signatur.split(",")) if letzte_signatur else set()
    if befunde - bekannt:
        return True
    return (now - zuletzt_gesendet).total_seconds() / 60 >= cooldown_min


async def _load_alert_state(conn) -> tuple[str | None, datetime | None]:
    row = await (await conn.execute(
        "SELECT signature, last_sent_at FROM watchdog_alert_state WHERE id = 1"
    )).fetchone()
    return (row[0], row[1]) if row else (None, None)


async def _store_alert_state(conn, signatur: str, now: datetime) -> None:
    await conn.execute(
        """
        INSERT INTO watchdog_alert_state (id, signature, last_sent_at)
        VALUES (1, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            signature    = EXCLUDED.signature,
            last_sent_at = EXCLUDED.last_sent_at
        """,
        (signatur, now),
    )


def _fmt_age(age_min: float | None) -> str:
    if age_min is None:
        return "keine Daten"
    if age_min < 90:
        return f"{age_min:.0f}min"
    if age_min < 48 * 60:
        return f"{age_min / 60:.1f}h"
    return f"{age_min / 1440:.1f}d"


async def _record(conn, source: str, last_ts, age_min, is_stale: bool, threshold_min: int) -> None:
    await conn.execute(
        """
        INSERT INTO system_health (source, last_data_ts, age_minutes, is_stale, threshold_min)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (source, last_ts, age_min, is_stale, threshold_min),
    )


async def _check_alarm_channel(conn) -> bool:
    """
    Prüft, ob der Alarmkanal überhaupt zustellen kann, und schreibt das Ergebnis
    nach system_health — damit es im Dashboard als Kachel erscheint.

    Der Grund für diese Prüfung: der Kanal war am 2026-07-04 stummgeschaltet
    (TELEGRAM_CHAT_ID geleert), weil zu enge Schwellen ihn zugemüllt hatten.
    Danach hat der Wächter wochenlang korrekt erkannt und niemandem gesagt —
    unter anderem einen 31-Stunden-Ausfall. Ein stummer Wächter ist gefährlicher
    als keiner, weil das Dashboard grün bleibt. Also muss die Stummschaltung
    selbst sichtbar sein und darf keine Log-Zeile bleiben.

    Gibt True zurück, wenn der Kanal stumm ist.
    """
    stumm = not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    await _record(conn, _KANAL_SOURCE, None, None, stumm, 0)
    if stumm:
        fehlt = []
        if not TELEGRAM_TOKEN:
            fehlt.append("TELEGRAM_TOKEN")
        if not TELEGRAM_CHAT_ID:
            fehlt.append("TELEGRAM_CHAT_ID")
        log.warning("data_watchdog: ALARMKANAL STUMM — %s nicht gesetzt. "
                    "Befunde bleiben im Dashboard, gehen aber an niemanden raus.",
                    " und ".join(fehlt))
    return stumm


async def _check_freshness(conn, now: datetime) -> list[tuple[str, float | None, int]]:
    """Schicht B. Gibt die stale Quellen zurück: (name, age_min, threshold)."""
    stale: list[tuple[str, float | None, int]] = []

    for name, src in SOURCES.items():
        try:
            row = await (await conn.execute(src.query)).fetchone()
        except Exception as e:
            # Eine kaputte Query darf die anderen 21 Prüfungen nicht verhindern.
            log.error("data_watchdog: Query für %s fehlgeschlagen: %s", name, e)
            continue

        last_ts = row[0] if row else None
        if last_ts is None:
            age_min = None
            is_stale = True
        else:
            age_min = round((now - last_ts).total_seconds() / 60, 1)
            is_stale = age_min > src.threshold_min

        await _record(conn, name, last_ts, age_min, is_stale, src.threshold_min)

        if is_stale:
            stale.append((name, age_min, src.threshold_min))
            log.warning("data_watchdog: STALE — %s (%s, Schwelle %s, %s)",
                        name, _fmt_age(age_min), _fmt_age(src.threshold_min), src.kind)
        else:
            log.info("data_watchdog: OK — %s (%s)", name, _fmt_age(age_min))

    return stale


async def _check_runs(conn, now: datetime) -> list[tuple[str, float | None, int]]:
    """
    Schicht A. Prüft für jeden Poller, wann er zuletzt ERFOLGREICH lief.

    Ein Poller, der nur noch mit Fehler durchläuft, gilt als nicht gelaufen —
    sonst würde ein Dauerfehler als Gesundheit gelesen.
    """
    stale: list[tuple[str, float | None, int]] = []

    # Wie lange sammelt poller_runs überhaupt schon? Ohne diese Frage meldet die
    # Lauf-Wache nach jedem Container-Neustart JEDEN täglichen Poller als tot —
    # er KANN dort noch keine Historie haben. Beim ersten scharfen Lauf traf das
    # dre_metrics, tick_collector.run_ohlcv und run_cleanup gleichzeitig.
    # Fehlende Historie ist keine Aussage über einen Ausfall (NULL ≠ neutral).
    try:
        row = await (await conn.execute("SELECT MIN(started_at) FROM poller_runs")).fetchone()
        sammelt_seit = row[0] if row else None
    except Exception as e:
        log.error("data_watchdog: poller_runs nicht lesbar: %s", e)
        return stale

    for poller, max_pause in POLLER_CADENCE.items():
        try:
            row = await (await conn.execute(
                "SELECT MAX(started_at) FROM poller_runs WHERE poller = %s AND ok",
                (poller,),
            )).fetchone()
        except Exception as e:
            log.error("data_watchdog: poller_runs-Query für %s fehlgeschlagen: %s", poller, e)
            continue

        last_run = row[0] if row else None
        if last_run is None:
            age_min = None
            # Erst wenn die Wache länger sammelt als der erlaubte Abstand, ist
            # das Fehlen eines Erfolgslaufs eine Aussage.
            beobachtet_min = (((now - sammelt_seit).total_seconds() / 60)
                              if sammelt_seit else 0)
            is_stale = beobachtet_min > max_pause
            if not is_stale:
                log.info("data_watchdog: %s — noch keine Historie "
                         "(Wache sammelt seit %s, erlaubt %s)",
                         poller, _fmt_age(beobachtet_min), _fmt_age(max_pause))
        else:
            age_min = round((now - last_run).total_seconds() / 60, 1)
            is_stale = age_min > max_pause

        await _record(conn, f"{_LAUF_PREFIX}{poller}", last_run, age_min, is_stale, max_pause)

        if is_stale:
            stale.append((f"{_LAUF_PREFIX}{poller}", age_min, max_pause))
            log.warning("data_watchdog: POLLER STILL — %s (letzter Erfolg: %s, erlaubt %s)",
                        poller, _fmt_age(age_min), _fmt_age(max_pause))

    return stale


def _build_alert(items: list[tuple[str, float | None, int]]) -> str:
    laeufe = [i for i in items if i[0].startswith(_LAUF_PREFIX)]
    daten  = [i for i in items if not i[0].startswith(_LAUF_PREFIX)]

    lines = ["⚠️ <b>MOUNT MIDAS — Wächter</b>"]
    if laeufe:
        lines.append("\n<b>Poller läuft nicht:</b>")
        for name, age, thr in laeufe:
            lines.append(f"• <code>{name[len(_LAUF_PREFIX):]}</code> — letzter Erfolg "
                         f"{_fmt_age(age)} (erlaubt {_fmt_age(thr)})")
    if daten:
        lines.append("\n<b>Daten stale:</b>")
        for name, age, thr in daten:
            lines.append(f"• <code>{name}</code> — {_fmt_age(age)} "
                         f"(Schwelle {_fmt_age(thr)})")
    return "\n".join(lines)


async def run() -> None:
    log.info("data_watchdog: start")
    now = datetime.now(timezone.utc)

    try:
        async with await psycopg.AsyncConnection.connect(_DSN) as conn:
            stumm = await _check_alarm_channel(conn)
            problems = await _check_runs(conn, now)
            problems += await _check_freshness(conn, now)

            # Befunde zuerst festschreiben, dann erst senden. Das Dashboard darf
            # nicht davon abhängen, ob Telegram erreichbar ist.
            await conn.commit()

            if stumm:
                # Bei stummem Kanal NICHT senden und den Cooldown NICHT setzen:
                # sonst verbrauchen die Alarme still ihre Sperrfrist und es
                # bliebe nach dem Entstummen eine weitere Stunde ruhig.
                if problems:
                    log.warning("data_watchdog: %d Befunde, aber Kanal stumm — "
                                "nur im Dashboard sichtbar", len(problems))
            else:
                # Gebündelt: EIN Alarm für alle aktuellen Befunde. Der Cooldown
                # hängt am ganzen Set, nicht an der einzelnen Quelle — sonst
                # zerfällt das Bündel wieder in einzelne Stundenrhythmen.
                signatur = _signature([p[0] for p in problems])
                letzte_signatur, zuletzt = await _load_alert_state(conn)
                if should_send({p[0] for p in problems}, letzte_signatur, zuletzt, now):
                    async with aiohttp.ClientSession() as session:
                        await _send_telegram(session, _build_alert(problems))
                    await _store_alert_state(conn, signatur, now)

            await conn.commit()

        log.info("data_watchdog: fertig — %d Quellen, %d Poller, %d Befunde, Kanal %s",
                 len(SOURCES), len(POLLER_CADENCE), len(problems),
                 "stumm" if stumm else "aktiv")

    except Exception:
        log.exception("data_watchdog: Fehler")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())

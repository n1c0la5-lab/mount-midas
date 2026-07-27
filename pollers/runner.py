"""
Mount Midas — Poller Runner
Schedules all pollers at their configured intervals.

Nebenläufigkeit: jeder geplante Lauf bekommt einen eigenen Thread, die
Schedule-Schleife bleibt frei. Vorher lief alles seriell in der Schleife, mit
zwei messbaren Folgen (Zahlen vom 27.07., aus poller_runs):

  1. time.sleep(60) NACH der Arbeit ⇒ Periode = 60s + Arbeitszeit. Die drei
     60s-Poller liefen dadurch im Schnitt alle 74s statt alle 60s (+24%).
  2. wallet_tracker (Schnitt 437s, max 505s) blockierte stündlich alles andere.
     Zwischen zwei Läufen der 60s-Poller lagen dadurch bis zu 9.5 Minuten;
     2.1% aller Takte lagen über 2 Minuten.

Das war nicht bloss unsauber, sondern der Grund für die weiten MM-10-Schwellen
der Schnell-Schicht: ob_snapshots und signal_log stehen auf 20 Minuten, gemessen
bei max 11 Minuten — und diese 11 Minuten waren der blockierte runner, kein
Marktbefund. Die Schwellen waren Notwehr gegen einen Nebenläufigkeitsfehler.

Warum Threads und nicht eine gemeinsame Event-Loop: jeder Poller bringt seine
eigene asyncio.run()-Welt mit, und run_stamp öffnet pro Stempel eine eigene
Verbindung. Threads sind hier der kleine Schnitt; ein Umbau auf eine einzige
Loop würde Scheduling, Erstlauf und Stempel-Pfad gleichzeitig anfassen.
Geprüft, bevor das hier gebaut wurde: kein Poller hält veränderlichen
Modul-Zustand, alle Modulwerte sind Konstanten, und es gibt keine geteilten
Clients oder Verbindungen.
"""
import asyncio
import logging
import threading
import time

import schedule

import correlation_calculator
import data_watchdog
import dre_metrics
import epz_calculator
import liq_poller
import master_agent
import neuron_poller
import np_poller
import ob_poller
import okx_liq_poller
import run_stamp
import signal_engine
import threshold_calculator
import tick_collector
import volume_profile_calculator
import wallet_tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def _poller_name(coro_fn) -> str:
    """z.B. tick_collector.run_market_data → 'tick_collector.run_market_data'."""
    module = getattr(coro_fn, "__module__", "") or ""
    name = getattr(coro_fn, "__qualname__", None) or getattr(coro_fn, "__name__", "unknown")
    return f"{module}.{name}" if module else name


async def _run_and_stamp(coro_fn, poller: str) -> None:
    """
    Führt den Poller aus und hinterlässt einen Lauf-Stempel in poller_runs
    (MM-10 Schicht A) — auch und besonders im Fehlerfall.

    rows_written wird nur gesetzt, wenn der Poller eine Zahl zurückgibt.
    Sonst NULL: "nicht gemeldet" ist eine ehrlichere Aussage als eine
    erfundene 0.
    """
    started = run_stamp.now()
    rows: int | None = None
    ok = False
    err: str | None = None
    try:
        result = await coro_fn()
        rows = result if isinstance(result, int) and not isinstance(result, bool) else None
        ok = True
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.error("poller error (%s): %s", poller, e)
    finally:
        await run_stamp.record_run(
            poller=poller,
            started_at=started,
            finished_at=run_stamp.now(),
            rows_written=rows,
            ok=ok,
            error=err,
        )


# Eine Sperre pro Poller. Sie verhindert, dass derselbe Poller mehrfach
# gleichzeitig läuft — sonst würde ein Poller, der langsamer ist als sein
# Intervall, sich unbegrenzt selbst aufstapeln. Genau das war bei seriellem
# Ablauf strukturell unmöglich und wird durch Threads erst denkbar.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(poller: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(poller, threading.Lock())


def is_overlapping(lock_frei: bool) -> bool:
    """
    Reine Entscheidung: muss dieser Takt übersprungen werden?

    Trivial, aber bewusst als eigene Funktion — so kann das Gate prüfen, dass
    ein belegter Poller wirklich übersprungen und nicht doch gestartet wird.
    """
    return not lock_frei


def execute(coro_fn, poller: str) -> None:
    """
    Führt einen Poller aus, höchstens einmal gleichzeitig.

    Ein übersprungener Takt wird als WARNING geloggt und NICHT gestempelt: er
    hat nicht stattgefunden, und ein Stempel dafür wäre eine Falschaussage über
    Schicht A. Die Lücke ist die ehrliche Meldung — läuft ein Poller dauerhaft
    über sein Intervall, meldet ihn der Wächter zu Recht.
    """
    lock = _lock_for(poller)
    war_frei = lock.acquire(blocking=False)
    if is_overlapping(war_frei):
        log.warning("runner: %s läuft noch — Takt übersprungen", poller)
        return
    try:
        asyncio.run(_run_and_stamp(coro_fn, poller))
    except Exception as e:
        # _run_and_stamp fängt Poller-Fehler selbst; hier landet nur, was
        # der Stempel-Pfad selbst wirft. Der Scheduler darf nie sterben.
        log.error("run_async error (%s): %s", poller, e)
    finally:
        lock.release()


def run_async(coro_fn):
    """
    Wrapper für schedule: startet den Poller in einem eigenen Thread und kehrt
    sofort zurück, damit die Schedule-Schleife frei bleibt.

    daemon=True, damit ein hängender Poller den Container-Stop nicht blockiert.
    """
    poller = _poller_name(coro_fn)

    def _wrapper():
        threading.Thread(target=execute, args=(coro_fn, poller),
                         name=poller, daemon=True).start()
    return _wrapper


def main():
    log.info("runner: starting Mount Midas pollers")

    # Schedules
    schedule.every(5).minutes.do(run_async(data_watchdog.run))
    schedule.every(30).minutes.do(run_async(volume_profile_calculator.run))
    schedule.every().day.at("04:00").do(run_async(np_poller.run))
    schedule.every().day.at("04:30").do(run_async(dre_metrics.run))
    schedule.every().day.at("05:00").do(run_async(threshold_calculator.run))
    schedule.every().day.at("00:05").do(run_async(tick_collector.run_ohlcv))
    schedule.every().hour.do(run_async(wallet_tracker.run))
    schedule.every().hour.do(run_async(tick_collector.run_market_data))
    schedule.every().hour.do(run_async(neuron_poller.run))
    schedule.every(60).seconds.do(run_async(ob_poller.run))
    schedule.every(60).seconds.do(run_async(tick_collector.run))
    schedule.every(15).minutes.do(run_async(liq_poller.run))
    schedule.every(5).minutes.do(run_async(okx_liq_poller.run))
    schedule.every(15).minutes.do(run_async(epz_calculator.run))
    schedule.every(15).minutes.do(run_async(correlation_calculator.run))
    schedule.every(60).seconds.do(run_async(signal_engine.run))
    schedule.every(5).minutes.do(run_async(master_agent.run))
    schedule.every().day.at("03:00").do(run_async(tick_collector.run_cleanup))

    # ── Sofortiger Erstlauf ───────────────────────────────────────────────────
    # ALLE Erstläufe laufen über run_async: einheitlich abgeschirmt und
    # gestempelt. Vorher war nur der letzte Block gegen Abstürze geschützt
    # (Lehre aus dem okx_liq_poller-Incident 2026-05-29) — die zehn Aufrufe
    # davor konnten den runner weiterhin töten, bevor die schedule loop
    # überhaupt startet. Und kein Erstlauf hinterließ eine Spur, wenn er
    # scheiterte: genau der Startzeitpunkt, an dem man sie am meisten braucht.
    #
    # Reihenfolge ist bedeutsam: np_poller füllt np_providers, worauf
    # wallet_tracker aufsetzt.
    initial_runs = (
        np_poller.run,
        wallet_tracker.run,
        neuron_poller.run,
        master_agent.run,
        ob_poller.run,
        tick_collector.run,
        liq_poller.run,
        okx_liq_poller.run,
        epz_calculator.run,
        tick_collector.run_market_data,
        tick_collector.run_ohlcv_backfill,
        volume_profile_calculator.run,
        correlation_calculator.run,
        threshold_calculator.run,
    )
    # data_watchdog: kein Erstlauf — erster Check nach 5min, wenn alle Poller laufen
    #
    # Die Sequenz läuft in EINEM Hintergrund-Thread: untereinander streng der
    # Reihe nach (np_poller vor wallet_tracker), aber ohne die Schedule-Schleife
    # aufzuhalten. Vorher blockierte sie den Start so lange, wie alle Erstläufe
    # zusammen brauchten — beim Deploy am 27.07. waren das über 8 Minuten, in
    # denen weder die 60s-Poller noch der Wächter liefen. Ausgerechnet direkt
    # nach einem Deploy, wo man am genauesten hinsehen will.
    def _initial_sequence() -> None:
        for fn in initial_runs:
            log.info("runner: initial run — %s", _poller_name(fn))
            execute(fn, _poller_name(fn))
        log.info("runner: initial runs done")

    threading.Thread(target=_initial_sequence, name="initial-runs",
                     daemon=True).start()

    log.info("runner: schedule loop started")
    log.info("  data_watchdog:        every 5min")
    log.info("  volume_profile:       every 30min (from spot_trades)")
    log.info("  ob_poller:      every 60s")
    log.info("  tick_collector: every 60s")
    log.info("  market_data:    every hour (funding rate + OI)")
    log.info("  wallet_tracker: every hour")
    log.info("  neuron_poller:  every hour")
    log.info("  ohlcv:          daily 00:05 UTC")
    log.info("  np_poller:      daily 04:00 UTC")
    log.info("  dre_metrics:    daily 04:30 UTC")
    log.info("  liq_poller:     every 15min")
    log.info("  okx_liq_poller: every 5min")
    log.info("  epz_calculator: every 15min")
    log.info("  correlation_calc: every 15min")
    log.info("  threshold_calc: daily 05:00 UTC")
    log.info("  signal_engine:  every 60s")
    log.info("  master_agent:   every 5min")
    log.info("  tick cleanup:   daily 03:00 UTC")

    # 1 Sekunde, nicht 60: schedule.run_pending() prüft nur, was fällig ist, und
    # startet Threads. Mit sleep(60) war die Periode jedes Jobs "60s PLUS die
    # Arbeit dieser Runde" — die drei 60s-Poller liefen dadurch gemessen alle
    # 74s statt alle 60s. Der Schlaf gehört nicht hinter die Arbeit, sondern
    # bestimmt nur, wie fein der Scheduler auflöst.
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()

"""
Mount Midas — Poller Runner
Schedules all pollers at their configured intervals.
"""
import asyncio
import logging
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


def run_async(coro_fn):
    """Wrapper so schedule (sync) can call async poller functions."""
    poller = _poller_name(coro_fn)

    def _wrapper():
        try:
            asyncio.run(_run_and_stamp(coro_fn, poller))
        except Exception as e:
            # _run_and_stamp fängt Poller-Fehler selbst; hier landet nur, was
            # der Stempel-Pfad selbst wirft. Der Scheduler darf nie sterben.
            log.error("run_async error (%s): %s", poller, e)
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
    for _fn in initial_runs:
        log.info("runner: initial run — %s", _poller_name(_fn))
        run_async(_fn)()

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

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()

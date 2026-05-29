"""
Mount Midas — Poller Runner
Schedules all pollers at their configured intervals.
"""
import asyncio
import logging
import time

import schedule

import data_watchdog
import dre_metrics
import epz_calculator
import liq_poller
import neuron_poller
import np_poller
import ob_poller
import okx_liq_poller
import signal_engine
import tick_collector
import volume_profile_calculator
import wallet_tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def run_async(coro_fn):
    """Wrapper so schedule (sync) can call async poller functions."""
    def _wrapper():
        try:
            asyncio.run(coro_fn())
        except Exception as e:
            log.error("poller error (%s): %s", coro_fn.__name__, e)
    return _wrapper


def main():
    log.info("runner: starting Mount Midas pollers")

    # Schedules
    schedule.every(5).minutes.do(run_async(data_watchdog.run))
    schedule.every(30).minutes.do(run_async(volume_profile_calculator.run))
    schedule.every().day.at("04:00").do(run_async(np_poller.run))
    schedule.every().day.at("04:30").do(run_async(dre_metrics.run))
    schedule.every().day.at("00:05").do(run_async(tick_collector.run_ohlcv))
    schedule.every().hour.do(run_async(wallet_tracker.run))
    schedule.every().hour.do(run_async(tick_collector.run_market_data))
    schedule.every().hour.do(run_async(neuron_poller.run))
    schedule.every(60).seconds.do(run_async(ob_poller.run))
    schedule.every(60).seconds.do(run_async(tick_collector.run))
    schedule.every(15).minutes.do(run_async(liq_poller.run))
    schedule.every(5).minutes.do(run_async(okx_liq_poller.run))
    schedule.every(15).minutes.do(run_async(epz_calculator.run))
    schedule.every(60).seconds.do(run_async(signal_engine.run))
    schedule.every().day.at("03:00").do(run_async(tick_collector.run_cleanup))

    # Sofortiger Erstlauf
    log.info("runner: initial run — np_poller")
    asyncio.run(np_poller.run())

    log.info("runner: initial run — wallet_tracker")
    asyncio.run(wallet_tracker.run())

    log.info("runner: initial run — neuron_poller")
    asyncio.run(neuron_poller.run())

    log.info("runner: initial run — ob_poller + tick_collector + liq_poller + okx_liq_poller + epz_calculator")
    asyncio.run(ob_poller.run())
    asyncio.run(tick_collector.run())
    asyncio.run(liq_poller.run())
    asyncio.run(okx_liq_poller.run())
    asyncio.run(epz_calculator.run())

    log.info("runner: initial run — market_data (funding + OI) + ohlcv backfill")
    asyncio.run(tick_collector.run_market_data())
    asyncio.run(tick_collector.run_ohlcv_backfill())

    log.info("runner: initial run — volume_profile_calculator")
    asyncio.run(volume_profile_calculator.run())
    # data_watchdog: kein Erstlauf — erster Check nach 5min, wenn alle Poller laufen

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
    log.info("  signal_engine:  every 60s")
    log.info("  tick cleanup:   daily 03:00 UTC")

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()

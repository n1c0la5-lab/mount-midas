-- Mount Midas — Migration 017
-- Drift-Backfill: Repo ⇄ Live-DB synchronisieren (Stand 2026-05-29).
-- Hintergrund: mehrere Tabellen/Spalten existierten live, aber in keiner
-- Migration (von untracked Pollern selbst angelegt). Diese Migration macht
-- ein frisches DB-Setup aus den Migrationen reproduzierbar.

-- 1) summary_stats — wird von correlation_calculator.py gefüttert
--    (bisher nur per CREATE IF NOT EXISTS im Poller, nicht in Migrationen).
CREATE TABLE IF NOT EXISTS summary_stats (
    id              SERIAL PRIMARY KEY,
    sell_price_corr DOUBLE PRECISION,
    oi_price_corr   DOUBLE PRECISION,
    best_lag_hours  INT,
    calculated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- 2) liquidation_events.exchange — live ergänzt, nie in Migrationen.
--    Multi-Exchange-Dedup-Constraint (ts, exchange, side, quantity_icp).
ALTER TABLE liquidation_events
    ADD COLUMN IF NOT EXISTS exchange TEXT NOT NULL DEFAULT 'okx';
CREATE UNIQUE INDEX IF NOT EXISTS uq_liq_events_ts_exchange_side_qty
    ON liquidation_events (ts, exchange, side, quantity_icp);

-- 3) predicted_fundings DROPPEN — Predicted Funding wurde komplett entfernt
--    (Panels 610–614 raus, kein Edge), einziger Feeder hl_poller.py gelöscht.
DROP TABLE IF EXISTS predicted_fundings;

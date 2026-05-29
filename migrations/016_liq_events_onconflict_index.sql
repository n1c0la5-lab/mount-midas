-- Mount Midas — Migration 016
-- Fix: okx_liq_poller crashte mit
--   "there is no unique or exclusion constraint matching the ON CONFLICT specification"
--
-- Ursache: okx_liq_poller nutzt ON CONFLICT (ts, side, quantity_icp), aber die
-- Live-Tabelle hatte nur den (nachträglich exchange-aware gewordenen) Constraint
-- (ts, exchange, side, quantity_icp). Migration 009 deklarierte zwar UNIQUE
-- (ts, side, quantity_icp), wurde aber via CREATE TABLE IF NOT EXISTS nie auf die
-- bereits existierende Tabelle angewendet.
--
-- Dieser Index stellt die vom Poller erwartete Conflict-Target wieder her.
-- (0 Duplikate zum Zeitpunkt der Anwendung verifiziert.)

CREATE UNIQUE INDEX IF NOT EXISTS uq_liq_events_ts_side_qty
    ON liquidation_events (ts, side, quantity_icp);

-- HINWEIS / offen: Repo-Migrationen sind ggü. der Live-DB im Drift —
-- die Spalte `liquidation_events.exchange` (DEFAULT 'okx') + der Constraint
-- (ts, exchange, side, quantity_icp) existieren live, aber in keiner Repo-Migration.
-- Sauberere Lösung später: okx_liq_poller exchange-aware machen
-- (INSERT ... exchange='okx' ... ON CONFLICT (ts, exchange, side, quantity_icp))
-- und diesen redundanten Index wieder droppen. Siehe Backlog.

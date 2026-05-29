-- Migration 015: Master Agent Log (MM-07)
-- Eigene Tabelle statt signal_log: master_agent darf NICHT in signal_log
-- schreiben, sonst korrumpiert es signal_engines last_score/last_regime-Logik
-- (die liest signal_log ORDER BY ts DESC LIMIT 1). Dient als Anti-Spam-State
-- (Change-Detection) und als Audit-Log der Empfehlungen.

CREATE TABLE IF NOT EXISTS master_agent_log (
    ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    recommendation TEXT NOT NULL,      -- LONG_SETUP | LONG_SETUP_SCHWACH | SHORT_SETUP | WARTEN
    regime         TEXT,               -- aus signal_log (NP-Flow Sub-Agent)
    neuron_signal  TEXT,               -- SUPPLY_PRESSURE_RISING | SUPPLY_RETREATING | NEUTRAL
    bullish_score  INT,
    bearish_score  INT,
    conflicts      TEXT,               -- komma-getrennt, leer wenn keine
    alerted        BOOLEAN DEFAULT FALSE,
    details        JSONB
);

CREATE INDEX IF NOT EXISTS idx_master_agent_log_ts
    ON master_agent_log (ts DESC);

-- Mount Midas — Migration 007
-- signal_log: regime Spalte für Regime-Modell (KOMPRESSION / TRIGGER_BULLISH / DISTRIBUTION / etc.)

ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS regime TEXT;
CREATE INDEX IF NOT EXISTS idx_signal_log_regime ON signal_log (regime);

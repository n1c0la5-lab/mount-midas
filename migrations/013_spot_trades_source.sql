-- Migration 013: spot_trades source column für Multi-Exchange CVD

ALTER TABLE spot_trades
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'binance';

CREATE INDEX IF NOT EXISTS spot_trades_source_ts_idx
  ON spot_trades (source, ts DESC);

-- Dedup-Index für HL trades (kein agg_trade_id vorhanden)
CREATE UNIQUE INDEX IF NOT EXISTS spot_trades_hl_dedup
  ON spot_trades (ts, price, quantity_icp)
  WHERE source = 'hl';

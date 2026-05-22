-- Mount Midas — Migration 003
-- liquidation_snapshots: Coinglass → Binance native endpoints
-- Tabelle ist leer, daher DROP + RECREATE

DROP TABLE IF EXISTS liquidation_snapshots;

CREATE TABLE liquidation_snapshots (
  id                      BIGSERIAL PRIMARY KEY,
  ts                      TIMESTAMPTZ NOT NULL,
  -- Binance: futures/data/globalLongShortAccountRatio
  global_long_pct         NUMERIC,      -- Anteil Long-Accounts (0.0–1.0)
  global_short_pct        NUMERIC,      -- Anteil Short-Accounts (0.0–1.0)
  global_ls_ratio         NUMERIC,      -- long/short Account Ratio
  -- Binance: futures/data/topLongShortAccountRatio
  top_long_pct            NUMERIC,
  top_short_pct           NUMERIC,
  top_ls_ratio            NUMERIC,      -- Top-Trader Long/Short Ratio
  -- Binance: futures/data/takerlongshortRatio
  taker_buy_sell_ratio    NUMERIC,      -- >1 = Käufer aggressiver, <1 = Verkäufer aggressiver
  taker_buy_vol_icp       NUMERIC,
  taker_sell_vol_icp      NUMERIC,
  -- Binance: fapi/v1/openInterest
  open_interest_icp       NUMERIC,      -- Gesamtes Open Interest in ICP
  -- Alert
  skew_alert              BOOLEAN DEFAULT FALSE
  -- skew_alert = TRUE wenn:
  --   global_ls_ratio > 1.50 (oberes Quartil, Markt sehr long-lastig)
  --   ODER top_ls_ratio > 1.75 (Top-Trader extrem positioniert)
);

CREATE INDEX ON liquidation_snapshots (ts DESC);

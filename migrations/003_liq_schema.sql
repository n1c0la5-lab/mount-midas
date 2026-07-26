-- Mount Midas — Migration 003
-- liquidation_snapshots: Coinglass → Binance native endpoints
-- Tabelle ist leer, daher DROP + RECREATE

-- Wiederholbar gemacht 2026-07-26 (MM-10): CREATE TABLE/INDEX ohne
-- IF NOT EXISTS liess diese Migration beim zweiten Lauf abbrechen bzw. legte
-- still Duplikat-Indizes an. Indexnamen sind aus der Live-DB uebernommen, damit
-- Repo und Live konvergieren. Belegt durch scripts/gate.sh (Doppellauf).

-- Der Reshape läuft NUR, solange die Tabelle noch die alte Coinglass-Form hat
-- (erkennbar an long_liq_usd, das es in der neuen Form nicht gibt).
--
-- Vorher stand hier ein nacktes DROP TABLE IF EXISTS. Das war korrekt, solange
-- Migrationen nur einmal beim Anlegen des Volumes liefen — mit
-- scripts/migrate.sh gegen eine laufende DB hätte es 5.926 echte Zeilen
-- gelöscht. Gefunden 2026-07-26 durch den Doppellauf-Test im Gate.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name   = 'liquidation_snapshots'
      AND column_name  = 'long_liq_usd'
  ) THEN
    RAISE NOTICE 'liquidation_snapshots: alte Coinglass-Form erkannt, Reshape';
    DROP TABLE liquidation_snapshots;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS liquidation_snapshots (
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

CREATE INDEX IF NOT EXISTS liquidation_snapshots_ts_idx ON liquidation_snapshots (ts DESC);

-- Mount Midas — ICP Agent Schema
-- Phase 1: Initial DB Setup

-- Node Provider Stammdaten
CREATE TABLE np_providers (
  principal        TEXT PRIMARY KEY,
  name             TEXT,
  node_count       INTEGER,
  reward_xdr       NUMERIC,
  reward_icp       NUMERIC,
  last_mint_at     TIMESTAMPTZ,
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Alle Transaktionen von NP Principals (hop_depth 0-3+)
CREATE TABLE wallet_movements (
  id               BIGSERIAL PRIMARY KEY,
  from_principal   TEXT NOT NULL,
  to_principal     TEXT NOT NULL,
  amount_icp       NUMERIC NOT NULL,
  tx_hash          TEXT UNIQUE,
  block_height     BIGINT,
  ts               TIMESTAMPTZ NOT NULL,
  is_exchange      BOOLEAN DEFAULT FALSE,
  cluster_id       INTEGER,
  hop_depth        INTEGER DEFAULT 0
);

CREATE INDEX ON wallet_movements (from_principal, ts);
CREATE INDEX ON wallet_movements (to_principal, ts);
CREATE INDEX ON wallet_movements (hop_depth);

-- Zieladressen die von mehreren NPs angesteuert werden (Trading Desk Kandidaten)
CREATE TABLE destination_clusters (
  id               SERIAL PRIMARY KEY,
  to_principal     TEXT UNIQUE NOT NULL,
  np_count         INTEGER DEFAULT 0,
  total_icp        NUMERIC DEFAULT 0,
  label            TEXT,
  is_trading_desk  BOOLEAN DEFAULT FALSE,
  first_seen_at    TIMESTAMPTZ,
  updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Binance Order Book Snapshots (±2% vom Mid-Price)
CREATE TABLE ob_snapshots (
  id               BIGSERIAL PRIMARY KEY,
  ts               TIMESTAMPTZ NOT NULL,
  bid_depth_icp    NUMERIC,
  ask_depth_icp    NUMERIC,
  spread_bps       NUMERIC,
  mid_price_usdt   NUMERIC
);

CREATE INDEX ON ob_snapshots (ts DESC);

-- ICP/USDT Spot Tick Data — Rolling 90 Tage
CREATE TABLE spot_trades (
  id               BIGSERIAL PRIMARY KEY,
  agg_trade_id     BIGINT UNIQUE,
  ts               TIMESTAMPTZ NOT NULL,
  price            NUMERIC NOT NULL,
  quantity_icp     NUMERIC NOT NULL,
  is_buyer_maker   BOOLEAN
);

CREATE INDEX ON spot_trades (ts DESC);
CREATE INDEX ON spot_trades (price, ts);

-- ICP/USDT Perpetual Tick Data — Rolling 90 Tage
CREATE TABLE perp_trades (
  id               BIGSERIAL PRIMARY KEY,
  agg_trade_id     BIGINT UNIQUE,
  ts               TIMESTAMPTZ NOT NULL,
  price            NUMERIC NOT NULL,
  quantity_icp     NUMERIC NOT NULL,
  is_buyer_maker   BOOLEAN
);

CREATE INDEX ON perp_trades (ts DESC);
CREATE INDEX ON perp_trades (price, ts);

-- Perp/Spot Volume Ratio — minütlich aggregiert
CREATE TABLE market_activity (
  id                   BIGSERIAL PRIMARY KEY,
  ts                   TIMESTAMPTZ NOT NULL,
  spot_volume_icp      NUMERIC,
  perp_volume_icp      NUMERIC,
  perp_spot_ratio      NUMERIC,
  activity_alert       BOOLEAN DEFAULT FALSE
);

CREATE INDEX ON market_activity (ts DESC);

-- Coinglass Long/Short Liquidation Ratio (alle 15min)
CREATE TABLE liquidation_snapshots (
  id               BIGSERIAL PRIMARY KEY,
  ts               TIMESTAMPTZ NOT NULL,
  long_liq_usd     NUMERIC,
  short_liq_usd    NUMERIC,
  ratio            NUMERIC,
  skew_alert       BOOLEAN DEFAULT FALSE
);

CREATE INDEX ON liquidation_snapshots (ts DESC);

-- Volume Profile: POC, VAH, VAL (täglich berechnet)
CREATE TABLE volume_profile (
  id               SERIAL PRIMARY KEY,
  calculated_at    TIMESTAMPTZ DEFAULT NOW(),
  lookback_days    INTEGER NOT NULL,
  poc_price        NUMERIC NOT NULL,
  vah_price        NUMERIC NOT NULL,
  val_price        NUMERIC NOT NULL,
  total_volume_icp NUMERIC
);

CREATE INDEX ON volume_profile (calculated_at DESC);

-- Manuelle Support/Resistance Levels
CREATE TABLE key_levels (
  id          SERIAL PRIMARY KEY,
  price_usdt  NUMERIC NOT NULL,
  level_type  TEXT,
  formed_at   TIMESTAMPTZ,
  active      BOOLEAN DEFAULT TRUE,
  notes       TEXT
);

-- Signal-Ereignisse mit Score (Alert-Trigger)
CREATE TABLE signal_log (
  id               BIGSERIAL PRIMARY KEY,
  ts               TIMESTAMPTZ DEFAULT NOW(),
  score            INTEGER NOT NULL,
  trigger_mint     BOOLEAN DEFAULT FALSE,
  trigger_wallet   BOOLEAN DEFAULT FALSE,
  trigger_ob_thin  BOOLEAN DEFAULT FALSE,
  icp_price_usdt   NUMERIC,
  ob_depth_icp     NUMERIC,
  details          JSONB,
  alerted          BOOLEAN DEFAULT FALSE
);

-- Seed: Manuell validierte Wallet-Kette (Sygnum Bank, Mai 2026)
INSERT INTO destination_clusters (to_principal, np_count, total_icp, label, is_trading_desk, first_seen_at)
VALUES
  ('64860a52...95fc9d', 1,  0, 'Sygnum Custody',               FALSE, NOW()),
  ('bef91947...93f5b6', 3,  0, 'Multi-NP Aggregator',          TRUE,  NOW()),
  ('134a1847...613b41', 3,  0, 'Weiterleitungs-Hub ($900k+)',   TRUE,  NOW());

-- Seed: Key Levels (manuell validiert, Mai 2026)
INSERT INTO key_levels (price_usdt, level_type, formed_at, notes)
VALUES
  (4.093, 'sweep_high',  '2026-05-01', 'ATH Mai 2026 — Liquiditätssweep'),
  (2.621, 'resistance',  '2026-04-17', 'Altes Hoch 08.04 + 17.04 — Support/Resistance Flip'),
  (2.400, 'support',     '2026-04-01', 'POC Akkumulationsphase — starke Gravitationszone'),
  (2.272, 'sweep_low',   '2026-04-08', 'Tief vor dem Pump');

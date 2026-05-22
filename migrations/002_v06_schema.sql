-- Mount Midas — Migration 002
-- v0.6: Threshold-Berechnung, geography/hw_generation, XDR-Rates

-- np_providers erweitern
ALTER TABLE np_providers ADD COLUMN IF NOT EXISTS hw_generation TEXT;
ALTER TABLE np_providers ADD COLUMN IF NOT EXISTS geography TEXT;

-- signal_log erweitern
ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS trigger_threshold BOOLEAN DEFAULT FALSE;
ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS mandatory_sell_icp NUMERIC;

-- XDR/USD Tageskurse (IMF-Quelle)
CREATE TABLE IF NOT EXISTS xdr_rates (
  id         SERIAL PRIMARY KEY,
  ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  rate       NUMERIC NOT NULL,   -- XDR/USD
  source     TEXT DEFAULT 'imf'
);
CREATE INDEX IF NOT EXISTS ON xdr_rates (ts DESC);

-- Offizielle NNS-Remuneration-Tabelle (statisch, nur bei NNS-Proposals updaten)
CREATE TABLE IF NOT EXISTS np_remuneration (
  id             SERIAL PRIMARY KEY,
  hw_generation  TEXT NOT NULL,
  geography      TEXT NOT NULL,
  reward_xdr     NUMERIC NOT NULL,
  opex_xdr_est   NUMERIC,
  capex_xdr_4y   NUMERIC,
  valid_from     TIMESTAMPTZ DEFAULT NOW(),
  notes          TEXT
);

-- Seed-Daten: Gen-1.1 (Stand Mai 2026, ICP Wiki)
INSERT INTO np_remuneration (hw_generation, geography, reward_xdr, notes) VALUES
  ('gen1_1', 'CH',           1136, 'Schweiz — Gen-1.1 Post-48-Monats-Modell'),
  ('gen1_1', 'EU_other',     1061, 'EU (exkl. Schweiz/Slowenien) — Gen-1.1'),
  ('gen1_1', 'Slovenia',     1152, 'Slowenien — Gen-1.1'),
  ('gen1_1', 'US_FL_GA_CA',  1072, 'USA Florida/Georgia/California — Gen-1.1'),
  ('gen1_1', 'US_other',     1004, 'USA andere Bundesstaaten — Gen-1.1'),
  ('gen1_1', 'Canada',       1088, 'Kanada — Gen-1.1'),
  ('gen1_1', 'Singapore',    1234, 'Singapur — Gen-1.1'),
  ('gen1_1', 'Japan',        1188, 'Japan — Gen-1.1'),
  ('gen1_1', 'non_eu_reloc', 1357, 'Nicht-EU Relokation (+10%) — Gen-1.1')
ON CONFLICT DO NOTHING;

-- Tägliche Pflicht-Liquidierungsberechnung pro NP
CREATE TABLE IF NOT EXISTS np_threshold_daily (
  id                  BIGSERIAL PRIMARY KEY,
  ts                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  principal           TEXT NOT NULL REFERENCES np_providers(principal),
  reward_xdr          NUMERIC NOT NULL,
  xdr_usd_rate        NUMERIC NOT NULL,
  icp_price_usdt      NUMERIC NOT NULL,
  opex_usd_est        NUMERIC NOT NULL,
  mandatory_sell_usd  NUMERIC NOT NULL,
  mandatory_sell_icp  NUMERIC NOT NULL,
  total_reward_icp    NUMERIC NOT NULL,
  discretionary_icp   NUMERIC NOT NULL,
  sell_pressure_ratio NUMERIC NOT NULL,
  node_count          INTEGER NOT NULL,
  geography           TEXT NOT NULL,
  hw_generation       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ON np_threshold_daily (ts DESC);
CREATE INDEX IF NOT EXISTS ON np_threshold_daily (principal, ts DESC);

-- Netzwerkweiter Sell-Pressure (Tages-Aggregat)
CREATE TABLE IF NOT EXISTS threshold_aggregate_daily (
  id                       BIGSERIAL PRIMARY KEY,
  ts                       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  xdr_usd_rate             NUMERIC NOT NULL,
  icp_price_usdt           NUMERIC NOT NULL,
  np_count                 INTEGER NOT NULL,
  total_reward_icp         NUMERIC NOT NULL,
  total_mandatory_sell_icp NUMERIC NOT NULL,
  total_discretionary_icp  NUMERIC NOT NULL,
  avg_sell_pressure_ratio  NUMERIC NOT NULL,
  mandatory_sell_at_2usd   NUMERIC,
  mandatory_sell_at_3usd   NUMERIC,
  mandatory_sell_at_5usd   NUMERIC
);
CREATE INDEX IF NOT EXISTS ON threshold_aggregate_daily (ts DESC);

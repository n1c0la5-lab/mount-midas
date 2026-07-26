-- Mount Midas — Migration 005
-- NP-Analyse: Performance, Reward Mints, Wallet Labels

-- Wiederholbar gemacht 2026-07-26 (MM-10): CREATE TABLE/INDEX ohne
-- IF NOT EXISTS liess diese Migration beim zweiten Lauf abbrechen bzw. legte
-- still Duplikat-Indizes an. Indexnamen sind aus der Live-DB uebernommen, damit
-- Repo und Live konvergieren. Belegt durch scripts/gate.sh (Doppellauf).

CREATE TABLE IF NOT EXISTS np_performance (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL,
    node_id             TEXT NOT NULL,
    provider_principal  TEXT NOT NULL,
    blocks_produced     BIGINT,
    blocks_failed       BIGINT,
    failure_rate_pct    REAL,
    uptime_pct          REAL,
    UNIQUE (ts, node_id)
);

CREATE INDEX IF NOT EXISTS np_performance_ts_idx ON np_performance (ts DESC);
CREATE INDEX IF NOT EXISTS np_performance_provider_principal_ts_idx ON np_performance (provider_principal, ts DESC);

CREATE TABLE IF NOT EXISTS np_reward_mints (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL,
    provider_principal  TEXT NOT NULL,
    amount_icp          REAL NOT NULL,
    reward_period       TEXT,  -- YYYY-MM
    UNIQUE (provider_principal, reward_period)
);

CREATE INDEX IF NOT EXISTS np_reward_mints_ts_idx ON np_reward_mints (ts DESC);
CREATE INDEX IF NOT EXISTS np_reward_mints_provider_principal_ts_idx ON np_reward_mints (provider_principal, ts DESC);

CREATE TABLE IF NOT EXISTS np_wallet_labels (
    principal   TEXT PRIMARY KEY,
    label       TEXT NOT NULL,  -- 'exchange', 'aggregator', 'custody', 'staking', 'unknown'
    exchange    TEXT,           -- 'binance', 'coinbase', 'kraken', etc.
    confirmed   BOOLEAN NOT NULL DEFAULT FALSE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

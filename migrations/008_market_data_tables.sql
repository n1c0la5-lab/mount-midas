-- Mount Midas — Migration 008
-- Funding Rate, Open Interest, OHLCV täglich (Binance REST)

CREATE TABLE IF NOT EXISTS funding_rates (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL,
    funding_rate   NUMERIC     NOT NULL,  -- z.B. 0.0001 = 0.01%
    next_funding_ts TIMESTAMPTZ,
    mark_price     NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_funding_rates_ts ON funding_rates (ts DESC);

CREATE TABLE IF NOT EXISTS open_interest (
    id      BIGSERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ NOT NULL,
    oi_icp  NUMERIC     NOT NULL,
    oi_usdt NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_open_interest_ts ON open_interest (ts DESC);

-- OHLCV täglich: ein Eintrag pro Handelstag (UTC Mitternacht = Binance daily close)
CREATE TABLE IF NOT EXISTS ohlcv_daily (
    date         DATE    PRIMARY KEY,
    open         NUMERIC NOT NULL,
    high         NUMERIC NOT NULL,
    low          NUMERIC NOT NULL,
    close        NUMERIC NOT NULL,
    volume_icp   NUMERIC NOT NULL,
    volume_usdt  NUMERIC
);

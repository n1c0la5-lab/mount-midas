-- Mount Midas — Migration 009
-- OKX Liquidation Events (Force Orders) — Long vs. Short

CREATE TABLE IF NOT EXISTS liquidation_events (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,
    side         TEXT        NOT NULL,  -- 'long' oder 'short' (wer liquidiert wurde)
    quantity_icp NUMERIC     NOT NULL,
    price        NUMERIC     NOT NULL,
    UNIQUE (ts, side, quantity_icp)
);
CREATE INDEX IF NOT EXISTS idx_liq_events_ts   ON liquidation_events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_liq_events_side ON liquidation_events (side, ts DESC);

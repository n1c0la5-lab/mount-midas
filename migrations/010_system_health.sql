-- Mount Midas — Migration 010
-- System Health: Watchdog-Status für alle Datenquellen

CREATE TABLE IF NOT EXISTS system_health (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source          TEXT        NOT NULL,
    last_data_ts    TIMESTAMPTZ,
    age_minutes     NUMERIC,
    is_stale        BOOLEAN     NOT NULL DEFAULT FALSE,
    threshold_min   INTEGER     NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_system_health_source_ts ON system_health (source, ts DESC);
CREATE INDEX IF NOT EXISTS idx_system_health_ts        ON system_health (ts DESC);

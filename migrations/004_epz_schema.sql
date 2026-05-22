-- Mount Midas — Migration 004
-- epz_scores: Extreme Pressure Zone Composite Score
-- Läuft alle 15 Minuten nach liq_poller, liest aus liquidation_snapshots + ob_snapshots

CREATE TABLE IF NOT EXISTS epz_scores (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Rohdaten (zur Nachvollziehbarkeit)
    price           DOUBLE PRECISION,   -- mid_price_usdt zum Berechnungszeitpunkt
    sell_ratio      DOUBLE PRECISION,   -- taker_sell / (buy+sell), letzter Wert
    sell_momentum   DOUBLE PRECISION,   -- recent_mean − older_mean (letzte 5 vs. 6–15 Perioden)
    price_drop_pct  DOUBLE PRECISION,   -- % Kursveränderung über letzte ~15 Minuten (negativ = Rückgang)
    oi_change_pct   DOUBLE PRECISION,   -- % OI-Veränderung über 3 Perioden ≈ 45 Minuten
    ls_ratio        DOUBLE PRECISION,   -- top_ls_ratio aus liquidation_snapshots

    -- Sub-Scores 0–100
    s_taker         DOUBLE PRECISION,
    s_momentum      DOUBLE PRECISION,
    s_delta         DOUBLE PRECISION,
    s_oi            DOUBLE PRECISION,
    s_ls            DOUBLE PRECISION,

    -- Composite
    extreme_score   DOUBLE PRECISION NOT NULL,
    is_extreme      BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX ON epz_scores (ts DESC);
CREATE INDEX ON epz_scores (is_extreme, ts DESC);

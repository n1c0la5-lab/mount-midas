-- Migration 014: Neuron Dissolve Queue (MM-08)
-- Struktureller ICP-Supply-Tracker aus NNS Governance-Metriken.
-- State-basiertes Schema (7d/30d-Buckets via IC-API nicht verfügbar — feinste
-- Zeitauflösung ist "<6 Monate"). Hauptsignal: Trend/Delta auf dissolving_icp.

CREATE TABLE IF NOT EXISTS neuron_dissolve_snapshots (
    ts                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dissolved_icp       BIGINT NOT NULL,   -- State=Dissolved: aufgelöst, sofort disbursable (Überhang-Stock)
    dissolving_icp      BIGINT NOT NULL,   -- State=Dissolving: aktiv im Countdown (Hauptsignal)
    not_dissolving_icp  BIGINT NOT NULL,   -- State=NotDissolving, <6mo: Kurzläufer, Countdown nicht gestartet
    lt_6mo_icp          BIGINT,            -- alle Neuronen mit dissolve delay < 6 Monate
    total_locked_icp    BIGINT,            -- gesamter gesperrter Stake (inkl. Langläufer)
    total_staked_icp    BIGINT,            -- gesamter Stake im NNS
    dissolving_count    INT,               -- Anzahl Dissolving-Neuronen
    neuron_count        INT                -- Gesamtzahl Neuronen
);

CREATE INDEX IF NOT EXISTS idx_neuron_dissolve_ts
    ON neuron_dissolve_snapshots (ts DESC);

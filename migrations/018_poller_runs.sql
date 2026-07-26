-- Mount Midas — Migration 018
-- MM-10 Datenwächter, Schicht A: Lauf-Stempel.
--
-- Hintergrund: system_health prüft nur max(ts) der Zieltabelle. Das kann
-- "Poller kaputt" nicht von "Markt still" unterscheiden — und es findet einen
-- Poller nicht, der zwar läuft, aber stillschweigend nichts schreibt
-- (dre_metrics hat so 12 Tage lang nichts getan, vgl. MM-10 Defekt 1).
--
-- poller_runs stempelt den Lauf selbst: lief er, wie lange, wie viele Zeilen
-- wurden GEMESSEN geschrieben, gab es einen Fehler.

CREATE TABLE IF NOT EXISTS poller_runs (
    id            BIGSERIAL PRIMARY KEY,
    poller        TEXT        NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    duration_ms   INTEGER,
    rows_written  INTEGER,
    ok            BOOLEAN     NOT NULL DEFAULT FALSE,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_poller_runs_poller_started
    ON poller_runs (poller, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_poller_runs_started
    ON poller_runs (started_at DESC);

-- np_reward_mints.fetched_at — wann DRE für diese Zeile zuletzt gelesen wurde.
--
-- Warum eine eigene Spalte und nicht ts: ts wird von threshold_calculator zur
-- Periodenauswahl benutzt. Würde ein Re-Fetch eines alten Monats dessen ts auf
-- jetzt setzen, wäre der alte Monat plötzlich der "neueste" — der Fix hätte
-- einen schlimmeren Fehler eingebaut als den, den er behebt.
--
-- fetched_at trägt die Vollständigkeits-Aussage: ein abgeschlossener Monat ist
-- erst dann final gespeichert, wenn er NACH Monatsende gelesen wurde.
ALTER TABLE np_reward_mints
    ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ;

-- Backfill: bestehende Zeilen haben ts == Lesezeitpunkt (so hat der alte Code
-- geschrieben), also ist ts hier die korrekte Herkunft für fetched_at.
UPDATE np_reward_mints SET fetched_at = ts WHERE fetched_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_np_reward_mints_period_fetched
    ON np_reward_mints (reward_period, fetched_at DESC);

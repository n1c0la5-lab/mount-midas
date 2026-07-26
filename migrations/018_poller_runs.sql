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

-- ── Drift-Nachtrag zu 017 ─────────────────────────────────────────────────────
--
-- funding_rates.exchange und open_interest.exchange existieren live (1433
-- binance- + 44 hl-Zeilen je Tabelle), aber in keiner Migration. Migration 017
-- wollte genau diese Art Drift beseitigen und hat die beiden übersehen.
--
-- Das ist nicht kosmetisch: das Hauptdashboard referenziert `exchange` an 19
-- Stellen. Ein Frisch-Setup aus den Migrationen hätte die Spalte nicht, die
-- Poller liefen weiter (sie benennen die Spalte nicht, der Default greift),
-- und 19 Panel-Queries wären kaputt — ohne dass ein Poller-Log etwas sagt.
--
-- Gefunden 2026-07-26 durch den Schema-Diff Migrationen ⇄ Live-DB.
-- Spalten- und Indexdefinitionen sind aus der Live-DB übernommen.
ALTER TABLE funding_rates
    ADD COLUMN IF NOT EXISTS exchange TEXT NOT NULL DEFAULT 'binance';
ALTER TABLE open_interest
    ADD COLUMN IF NOT EXISTS exchange TEXT NOT NULL DEFAULT 'binance';

CREATE INDEX IF NOT EXISTS idx_funding_rates_exchange
    ON funding_rates (exchange, ts DESC);
CREATE INDEX IF NOT EXISTS idx_oi_exchange
    ON open_interest (exchange, ts DESC);

-- Hinweis, kein Nachtrag: In Migration 002 fehlten vier CREATE-INDEX-Statements
-- die Namen ("CREATE INDEX IF NOT EXISTS ON ..." ist Syntaxfehler — mit
-- IF NOT EXISTS ist der Name Pflicht). Die Live-DB hat diese Indizes korrekt
-- benannt, die Repo-Datei war also die abgedriftete Kopie. 002 wurde auf die
-- Live-Namen gezogen; hier ist deshalb nichts nachzuholen.

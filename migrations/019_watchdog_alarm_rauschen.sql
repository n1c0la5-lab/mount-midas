-- 019 — Alarm-Rauschen abstellen (MM-10 Nachtrag)
--
-- Drei Dinge, alle aus Messung, nicht aus Schätzung:
--
-- 1. np_performance.fetched_at
--    np_performance hatte keinen Schreibstempel. Der Wächter musste die Frische
--    über MAX(ts) prüfen — aber ts ist der Metrik-TAG, und DRE-Belohnungsperioden
--    laufen vom 14. zum 14., nicht über Kalendermonate (Ausgabeordner heisst
--    "rewards_2026-06-14_to_2026-07-14"). Der neueste verfügbare Tag bleibt damit
--    strukturell bis zu einem Monat stehen, bis die nächste Periode schliesst.
--    Die Schwelle von 72h war als "begründete Obergrenze, KEIN gemessener Wert"
--    dokumentiert — sie lag um den Faktor 10 daneben und erzeugte rund um die Uhr
--    Fehlalarme fuer eine gesunde Tabelle.
--    Mit fetched_at gibt es denselben ehrlichen Schreibstempel wie bei
--    np_reward_mints, und die Frage "hat der Poller geschrieben?" ist wieder
--    taeglich beantwortbar statt monatlich.
--
-- 2. watchdog_alert_state
--    Der Alarm-Cooldown lag im Arbeitsspeicher UND pro Quelle. Beides falsch:
--    - im Arbeitsspeicher: jeder Deploy setzt ihn zurueck, dieselben Befunde
--      gehen erneut raus.
--    - pro Quelle: Quellen driften in eigene Stundenrhythmen, sobald eine
--      spaeter stale wird als die andere. Genau so entstanden am 27.07. die
--      Paare im Abstand von 5,5 Minuten — die Buendelung ("ein Alarm fuer
--      alles") war ausgehebelt, es gab N Nachrichten pro Stunde statt einer.
--    Der Cooldown gehoert deshalb an das BEFUND-SET, und er muss Neustarts
--    ueberleben.
--
-- 3. epz_scores_extreme_idx
--    Exakter Doppelgaenger von epz_scores_is_extreme_ts_idx (beide
--    btree (is_extreme, ts DESC)). Nur der zweite steht in 004_epz_schema.sql;
--    der erste ist irgendwann von Hand entstanden. Kostet Schreib-Last und
--    Platz, bringt nichts.

ALTER TABLE np_performance ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ;

-- Bestandszeilen bekommen bewusst KEINEN Wert: sie wurden geschrieben, bevor es
-- den Stempel gab. Ein Backfill mit now() wuerde eine Frische behaupten, die
-- nicht gemessen wurde (NULL ist nicht neutral, aber erfundene Daten sind
-- schlimmer). Der erste dre_metrics-Lauf nach dem Deploy stempelt die aktuelle
-- Periode ohnehin — bis dahin meldet der Waechter die Quelle als stale, was
-- korrekt ist: es LIEGT noch kein Schreibnachweis vor.

CREATE TABLE IF NOT EXISTS watchdog_alert_state (
    id           INTEGER     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    signature    TEXT        NOT NULL,
    last_sent_at TIMESTAMPTZ NOT NULL
);

DROP INDEX IF EXISTS epz_scores_extreme_idx;

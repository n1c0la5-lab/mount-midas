# EPZ Grafana Panels — SQL Queries

## Panel 1 — EPZ Score Kachel (Stat Panel)

**Visualization:** Stat
**Datasource:** PostgreSQL (mount-midas-db)

```sql
SELECT
  NOW()          AS time,
  extreme_score  AS "EPZ Score",
  CASE WHEN is_extreme THEN 'EXTREME ZONE' ELSE 'Normal' END AS "Status"
FROM epz_scores
ORDER BY ts DESC
LIMIT 1
```

**Thresholds:**
- 0   → grün   (#73BF69)
- 50  → gelb   (#FADE2A)
- 76  → rot    (#F2495C)

**Field: EPZ Score**
- Unit: `none`
- Decimals: 1
- Min: 0, Max: 100

---

## Panel 2 — Sub-Scores Breakdown (Bar Gauge)

Zeigt die 5 gewichteten Einzel-Scores auf einen Blick.

```sql
SELECT
  NOW() AS time,
  s_taker    * 0.32 AS "Taker Sell (32%)",
  s_momentum * 0.23 AS "Momentum (23%)",
  s_delta    * 0.20 AS "Price Drop (20%)",
  s_oi       * 0.15 AS "OI Change (15%)",
  s_ls       * 0.10 AS "L/S Shift (10%)"
FROM epz_scores
ORDER BY ts DESC
LIMIT 1
```

**Visualization:** Bar Gauge, Orientation: Horizontal
**Max:** 32 / 23 / 20 / 15 / 10 (je Signal-Gewicht × 100)

---

## Panel 3 — EPZ Punkte im Preischart

Im bestehenden ICP/USDT Preischart (Key Levels Panel) eine **zweite Query** hinzufügen:

```sql
SELECT
  ts          AS time,
  price       AS "EPZ Zone"
FROM epz_scores
WHERE is_extreme = true
  AND ts >= $__timeFrom()
  AND ts <= $__timeTo()
ORDER BY ts
```

**Series-Override für "EPZ Zone":**
- Draw mode: Points only
- Point size: 10
- Color: #F2495C (rot)
- Line width: 0
- Fill opacity: 0

→ Rote Punkte erscheinen genau am Kurs-Niveau wo EPZ gefeuert hat.

---

## Panel 4 — EPZ Score Verlauf (Time Series)

```sql
SELECT
  ts             AS time,
  extreme_score  AS "EPZ Score",
  76             AS "Schwellenwert"
FROM epz_scores
WHERE ts >= $__timeFrom()
  AND ts <= $__timeTo()
ORDER BY ts
```

**Series-Override "Schwellenwert":**
- Line style: Dashed
- Color: #F2495C
- Fill opacity: 0

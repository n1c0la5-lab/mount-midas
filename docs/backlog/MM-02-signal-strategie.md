---
titel: Signal-Strategie & Regime-Modell
kürzel: MM-02
status: in-progress
priorität: hoch
bereich: mount-midas / signal / dashboard
erstellt: 2026-05-23
---

# Signal-Strategie & Regime-Modell

## Ziel

Das Regime-Modell ist die zentrale Analyse-Schicht von Mount Midas. Statt permanent einen Score zu berechnen, wird erkannt **in welcher Marktphase wir sind** und was das nächste erwartete Ereignis ist.

Vollständiger Spec: `code-schmiede/projekte/mount-midas/docs/MM-signal-strategie-spec.md`

---

## Kontext: Was bereits läuft

| Vorhanden | Erledigt |
|-----------|----------|
| `detect_regime()` in `signal_engine.py` | 2026-05-20 |
| `regime`-Spalte in `signal_log` + Backfill (1665 Zeilen) | 2026-05-20 |
| Tages-Flow Query für Echtzeit-Regime | 2026-05-20 |
| Regime-Panel in Grafana Row 0 (Panel 11–15) | 2026-05-20 |
| Exchange-Whitelist (14 Adressen, hop1-Join) | 2026-05-20 |
| OHLCV täglich + Backfill 499 Tage | 2026-05-20 |
| Funding Rate Poller stündlich → `funding_rates` | 2026-05-20 |
| Open Interest Poller stündlich → `open_interest` | 2026-05-20 |
| `detect_regime()` um Funding Rate erweitert | 2026-05-21 |
| Trigger ⑨ CVD-Divergenz | 2026-05-23 |
| Trigger ⑩ Large Sell Cluster (≥3 × >6k ICP / 30min + CVD neg.) | 2026-05-23 |
| Trigger ⑪ Funding Rate Spike (> 0.0005) | 2026-05-23 |
| Score-Cooldown (2h Standard, 30min bei ⑨+⑩) | 2026-05-23 |

---

## Was noch fehlt

### Baustein 1 — Grafana: Fehlende Marktdaten-Panels

**Funding Rate Panel**
- Timeseries stündlich aus `funding_rates`
- Aktueller Wert als Stat
- Flip-Signal (Vorzeichenwechsel) visuell markieren

**Open Interest Panel**
- Timeseries stündlich aus `open_interest`
- OI-Anstieg/Rückgang als Balken

**OHLCV Preis-Chart**
- Kerzen-Chart täglich aus `ohlcv_daily`
- Regime-Farbe als Hintergrund-Band (Kompression = grün, Distribution = rot)

### Baustein 2 — Grafana: Regime-History Panel (Row 3)

State Timeline: Farbkodierung je Phase über Zeit (aus `signal_log.regime`).

Ziel-Layout laut Spec:
```
ROW 3 — HISTORY
  Signal Score Verlauf │ EPZ Verlauf │ Regime-History (State Timeline)
```

### Baustein 3 — Schwellenwerte Live-Kalibrierung

Die kalibrierten Schwellen aus dem Backtesting (Abschnitt 3 des Specs) müssen in `signal_engine.py` als Konstanten hinterlegt und gegen Live-Daten validiert werden:

| Schwelle | Aktuell | Spec-Ziel |
|----------|---------|-----------|
| KOMPRESSION_STARK | < 10% flow | Validieren |
| TRIGGER: großes Vol. | > 1M ICP + < 10% flow | Validieren |
| TOP-Signal | > 90% daily flow | Validieren |
| Large Sell Cluster | ≥ 3 × > 6.000 ICP / 30min | Beobachten |

---

## Umsetzungsreihenfolge

```
Phase 1 — Grafana Panels (1 Session)
  ├── Funding Rate Panel (Timeseries + Flip-Marker)
  ├── OI Panel (Timeseries)
  └── OHLCV Kerzen-Chart + Regime-Hintergrund

Phase 2 — Regime-History (0.5 Session)
  └── State Timeline Panel aus signal_log.regime

Phase 3 — Live-Kalibrierung (laufend)
  └── Schwellenwerte nach 30 Tagen Live-Betrieb anpassen
```

---

## Offene Fragen

- [ ] Backtesting 2022: Haben wir NP-Muster vor dem ATH? (OHLCV ab Jan 2025 — 2022 fehlt noch)
- [ ] `threshold_calculator.py` OPEX_RATIO: wann wird `opex_xdr_est` verfügbar?
- [ ] Large Sell Cluster: passen 6k/3er-Cluster/30min nach 30 Tagen Live?

---

## Verknüpfungen

- [[MM-01-icp-np-analyse]] — NP-Daten als Regime-Input
- [[MM-03-telegram-llm-alerts]] — Alert-Output auf Basis des Regimes
- [[MM-signal-strategie-spec]] — Vollständiger Spec mit Backtesting-Daten

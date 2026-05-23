# Mount Midas — Architektur

## Stack

```
Devbox (192.168.10.137)
├── mount-midas-db        Postgres 16 (Port 5434)
├── mount-midas-pollers   Python-Container (alle Poller + Signal Engine)
└── mount-midas-grafana   Grafana 11 (Port 3000)
```

Ollama läuft als systemd-Service auf der Devbox (Port 11434), außerhalb Docker.

---

## Poller — Zuständigkeit

| Datei | Takt | Schreibt in |
|-------|------|-------------|
| `tick_collector.py` | 60s | `spot_trades`, `perp_trades`, `market_activity` |
| `tick_collector.py` | stündlich | `funding_rates`, `open_interest` |
| `tick_collector.py` | täglich 00:05 | `ohlcv_daily` |
| `ob_poller.py` | 60s | `order_book_snapshots` |
| `liq_poller.py` | 15min | `liquidations` |
| `epz_calculator.py` | 15min | `epz_snapshots` |
| `np_poller.py` | täglich 04:00 | `node_providers`, `np_rewards` |
| `wallet_tracker.py` | stündlich | `wallet_movements` |
| `dre_metrics.py` | täglich 04:30 | `np_performance` |
| `signal_engine.py` | 60s | `signal_log` (liest alle Tabellen) |

---

## Signal Engine — Trigger-Übersicht

Score läuft auf /11. Alert bei Score ≥ SCORE_ALERT (konfigurierbar).

| # | Trigger | Quelle | Schwelle |
|---|---------|--------|---------|
| ① | OB Thin | `order_book_snapshots` | Bid/Ask-Volumen < Threshold |
| ② | Sell Ratio | `spot_trades` | Sell% im 2h-Fenster |
| ③ | Post-Mint Flow | `wallet_movements` | NP → Exchange within 7d |
| ④ | Wallet Hop | `wallet_movements` | Hop-2/3-Volumen steigend |
| ⑤ | EPZ | `epz_snapshots` | EPZ-Ratio > Threshold |
| ⑥ | Liquidation | `liquidations` | Long-Liq-Volumen |
| ⑦ | OI Spike | `open_interest` | OI-Anstieg > Threshold |
| ⑧ | OHLCV | `ohlcv_daily` | Tageskerzen-Pattern |
| ⑨ | CVD-Divergenz | `spot_trades` | Preis steigt, CVD fällt |
| ⑩ | Large Sell Cluster | `spot_trades` | ≥3 Sells > 6k ICP in 30min + CVD negativ |
| ⑪ | Funding Spike | `funding_rates` | Rate > 0.0005 |

Cooldown: 2h Standard, 30min wenn ⑨+⑩ gleichzeitig aktiv.

---

## DB-Tabellen (Migrations)

| Migration | Tabellen |
|-----------|---------|
| `001_init.sql` | `spot_trades`, `perp_trades`, `market_activity`, `order_book_snapshots` |
| `002_v06_schema.sql` | Schema-Erweiterungen v0.6 |
| `003_liq_schema.sql` | `liquidations` |
| `004_epz_schema.sql` | `epz_snapshots` |
| `005_np_schema.sql` | `node_providers`, `np_rewards`, `wallet_movements`, `np_wallet_labels` |
| `006_np_wallet_labels_account_id.sql` | account_id Spalte |
| `007_signal_log_regime.sql` | `signal_log` + `regime`-Spalte |
| `008_market_data_tables.sql` | `funding_rates`, `open_interest`, `ohlcv_daily` |

---

## Grafana — Dashboards

| Datei | UID | Inhalt |
|-------|-----|--------|
| `mount_midas_main.json` | (main) | Haupt-Dashboard: Signal, Market, NP-Flow |
| `icp_signal.json` | — | Signal-History |
| `icp_agent.json` | — | ICP Agent-Metriken |
| `korrelation_edge.json` | — | Korrelations-Analyse |
| `ops_signal.json` | — | Ops & Signal-Log |

Provisioning: `grafana/provisioning/` — `updateIntervalSeconds: 30`.
**Wichtig:** Grafana-API-Workflow einhalten (siehe CONTRIBUTING.md).

---

## Externe Abhängigkeiten

| Service | URL | Zweck |
|---------|-----|-------|
| Binance Spot API | `api.binance.com` | aggTrades, OHLCV |
| Binance Perp API | `fapi.binance.com` | aggTrades, Funding, OI |
| ICP Ledger | `2g62z-...icp0.io` | Wallet-Transaktionen |
| DRE CLI | binary auf Devbox | Node-Performance-Metriken |
| Ollama | `localhost:11434` | LLM-Kommentare (geplant MM-02) |
| Telegram Bot API | `api.telegram.org` | Alert-Nachrichten |

---

## Deploy-Flow

```
Lokal (Feature-Branch)
  → git push origin feat/...
  → git push devbox feat/...         ← post-receive Hook startet Container neu
  → Grafana: grafana-sync.sh + grafana-push.sh
```

Kein scp. Kein manuelles docker exec. Alles über Git.

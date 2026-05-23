---
titel: LLM Telegram Alerts
kürzel: MM-02
status: open
priorität: hoch
bereich: mount-midas / signal / ai
erstellt: 2026-05-23
---

# LLM Telegram Alerts

## Ziel

Die Telegram-Alerts von Mount Midas sollen nicht nur rohe Trigger-Listen zeigen,
sondern einen kurzen LLM-generierten Kontext-Kommentar enthalten:
Was bedeutet dieser Score konkret? Worauf jetzt achten?

Das LLM läuft bereits als Service auf der Devbox (Ollama, Port 11434).

---

## Kontext: Was bereits läuft

| Vorhanden | Zweck |
|-----------|-------|
| `signal_engine.py` → `_build_alert()` | Generiert strukturierten Telegram-Text (pure Python) |
| `_send_telegram()` | Schickt Text an Telegram Bot |
| Ollama auf Devbox | `qwen2.5:7b-instruct` + `mistral:7b` als Services |
| Trigger ①–⑪ + Score/11 | Vollständige Signal-Evaluation |

Die LLM-Integration existierte vor dem ersten Git-Commit — sie wurde beim
initialen Repo-Setup überschrieben (2026-05-23 incident).

---

## Was gebaut werden soll

### Neue Funktion: `_llm_comment()`

Nach `_build_alert()` wird ein 2-3 Satz LLM-Kommentar angehängt.

**Input (Prompt):**
```
Du bist ein ICP-Marktanalyst. Gib in 2-3 Sätzen auf Deutsch eine präzise
Einschätzung basierend auf diesem Signal:

Score: {score}/11
Regime: {regime}
Aktive Trigger: {active_triggers}
CVD (1h): {cvd_net_1h} ICP
Funding Rate: {funding_rate}
Large Sell Count (30min): {sell_count}

Was bedeutet das konkret? Worauf jetzt achten?
```

**Output:** 2-3 Sätze, kein Markdown, direkter Ton.

**Modell:** `qwen2.5:7b-instruct` (schneller als mistral für kurze Outputs)

### API-Call

```python
POST http://192.168.10.137:11434/api/generate
{
  "model": "qwen2.5:7b-instruct",
  "prompt": "...",
  "stream": false,
  "options": {"temperature": 0.3, "num_predict": 150}
}
```

### Fehler-Verhalten

- Ollama nicht erreichbar → Alert wird trotzdem geschickt, ohne LLM-Block
- Timeout (> 10s) → Alert wird ohne LLM-Block geschickt
- Kein OLLAMA_URL in `.env` → Feature deaktiviert (silent fallback)

### Alert-Format nach Integration

```
⚠️ MOUNT MIDAS ALERT — Score 7/11
Regime: Distribution
ICP/USDT: $8.42  |  Sell-Ratio: 68%

Aktive Trigger:
  🔴 CVD-Divergenz
  🔴 Large Sell Cluster (3× in 30min)
  🔴 OB Thin
  ...

─────────────────
📊 Analyse: Der CVD-Rückgang bei steigendem Preis deutet auf
institutionelles Selling in die Retail-Euphorie hin. Der
Large-Sell-Cluster bestätigt das — typisches Distribution-Muster
vor einem schnellen Retest der Unterstützung.
```

---

## Umsetzung (1 Feature-Branch)

```
Branch: feat/llm-telegram-alerts

1. .env.example: OLLAMA_URL=http://192.168.10.137:11434 hinzufügen
2. signal_engine.py: _llm_comment(score, t, meta, regime) → str | None
3. signal_engine.py: _build_alert() um LLM-Block erweitern (wenn vorhanden)
4. Timeout: 10s, silent fallback wenn Ollama nicht antwortet
5. Test: manuell via python -c "import asyncio; from signal_engine import ..."
```

---

## Offene Fragen

- [ ] Welches Modell war ursprünglich verwendet? (qwen2.5 oder mistral?)
- [ ] War der Prompt auf Deutsch oder Englisch?
- [ ] Soll der LLM-Block bei jedem Alert kommen oder nur ab Score ≥ 7?

---

## Verknüpfungen

- [[signal_engine]] — Implementierungspunkt
- [[MM-01-icp-np-analyse]] — NP-Sell-Pressure als Input für LLM-Prompt

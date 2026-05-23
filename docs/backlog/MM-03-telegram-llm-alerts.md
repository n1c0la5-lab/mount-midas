---
titel: Telegram Alerts + LLM-Kommentare
kürzel: MM-03
status: open
priorität: hoch
bereich: mount-midas / signal / ai
erstellt: 2026-05-23
---

# Telegram Alerts + LLM-Kommentare

## Ziel

Die Telegram-Alerts von Mount Midas sollen zwei Dinge leisten:

1. **Strukturiert:** Klarer Alert-Block mit Score, Regime, aktiven Triggern, Marktdaten
2. **Interpretiert:** 2–3 Satz LLM-Kommentar — was bedeutet dieser Score konkret, worauf jetzt achten

Das LLM läuft bereits als Service auf der Devbox (Ollama, Port 11434). Die LLM-Integration existierte vor dem ersten Git-Commit, wurde beim initialen Repo-Setup überschrieben (Incident 2026-05-23).

---

## Kontext: Was bereits läuft

| Vorhanden | Zweck |
|-----------|-------|
| `signal_engine.py` → `_build_alert()` | Strukturierter Telegram-Text (pure Python) |
| `_send_telegram()` | Sendet Text an Telegram Bot |
| Score-Cooldown (2h / 30min bei ⑨+⑩) | Verhindert stündliche Wiederholung |
| Ollama auf Devbox | `qwen2.5:7b-instruct` + `mistral:7b` als Services |
| Trigger ①–⑪ + Score/11 + Regime | Vollständige Signal-Evaluation |

---

## Was gebaut werden soll

### Teil 1 — Alert-Format überarbeiten

Der aktuelle `_build_alert()` Output ist funktional aber minimal. Ziel:

```
⚠️ MOUNT MIDAS ALERT — Score 7/11
Regime: Distribution
ICP/USDT: $8.42  |  Sell-Ratio: 68%  |  Funding: +0.032%

Aktive Trigger:
  🔴 ⑨ CVD-Divergenz
  🔴 ⑩ Large Sell Cluster (3× in 30min)
  🔴 ① OB Thin

─────────────────
📊 Analyse: Der CVD-Rückgang bei steigendem Preis deutet auf
institutionelles Selling in die Retail-Euphorie hin. Der
Large-Sell-Cluster bestätigt das — typisches Distribution-Muster
vor einem schnellen Retest der Unterstützung. Shortgelegenheit
bei nächster Erholungsrallye prüfen.
```

Änderungen gegenüber aktuellem Stand:
- Funding Rate im Header hinzufügen
- Trigger-Nummern ①–⑪ im Text
- LLM-Block als eigener Abschnitt (nur wenn Ollama erreichbar)

### Teil 2 — LLM-Kommentar (`_llm_comment()`)

**Neue Funktion in `signal_engine.py`:**

```python
async def _llm_comment(score, t, meta, regime) -> str | None:
    """Generiert 2-3 Satz Kontext-Kommentar via Ollama. None wenn nicht verfügbar."""
```

**Prompt (Deutsch, präzise):**
```
Du bist ein ICP-Marktanalyst. Gib in 2–3 Sätzen eine präzise Einschätzung.
Kein Markdown. Direkter Ton.

Score: {score}/11 | Regime: {regime}
Aktive Trigger: {aktive_trigger_namen}
CVD (1h): {cvd_net_1h} ICP | Funding: {funding_rate} | Large Sells (30min): {sell_count}

Was bedeutet das konkret? Worauf jetzt achten?
```

**Modell:** `qwen2.5:7b-instruct`

**API-Call:**
```
POST http://192.168.10.137:11434/api/generate
{"model": "qwen2.5:7b-instruct", "prompt": "...", "stream": false,
 "options": {"temperature": 0.3, "num_predict": 150}}
```

**Fehler-Verhalten (Silent Fallback):**
- Ollama nicht erreichbar → Alert ohne LLM-Block (kein Fehler, kein Log-Spam)
- Timeout > 10s → Alert ohne LLM-Block
- Kein `OLLAMA_URL` in `.env` → Feature deaktiviert

### Teil 3 — Konfiguration

`.env.example` erweitern:
```bash
OLLAMA_URL=http://192.168.10.137:11434
OLLAMA_MODEL=qwen2.5:7b-instruct
```

---

## Umsetzung (1 Feature-Branch: `feat/llm-telegram-alerts`)

```
1. .env.example: OLLAMA_URL + OLLAMA_MODEL hinzufügen
2. signal_engine.py: _llm_comment(score, t, meta, regime) → str | None
3. signal_engine.py: _build_alert() um Funding Rate + LLM-Block erweitern
4. signal_engine.py: Trigger-Nummern ①–⑪ in TRIGGER_LABELS eintragen
5. Timeout: aiohttp.ClientTimeout(total=10), silent fallback
6. Manuell testen: python3 -c "import asyncio; ..."
7. Deploy via: bash scripts/deploy.sh
```

---

## Offene Fragen

- [ ] Welches Modell war ursprünglich verwendet? (qwen2.5 oder mistral?)
- [ ] Soll der LLM-Block bei jedem Alert kommen oder nur ab Score ≥ 7?
- [ ] Prompt auf Deutsch oder Englisch? (Vermutung: Deutsch)

---

## Verknüpfungen

- [[MM-02-signal-strategie]] — Regime als Haupt-Input für LLM-Prompt
- [[MM-01-icp-np-analyse]] — NP-Sell-Pressure als Alert-Kontext

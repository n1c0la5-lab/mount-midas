# Mount Midas — Entwicklungs-Workflow

## Goldene Regel

**Nichts lebt nur auf der Devbox. Alles was nicht im Repo ist, existiert nicht.**

---

## Git-Workflow

### Feature Branches — immer, keine Ausnahme

```bash
# Neuer Branch vor jeder Änderung
git checkout -b feat/llm-telegram-alerts
git checkout -b fix/signal-cooldown
git checkout -b docs/architecture

# Nie direkt auf main committen
```

Branch-Naming:
| Präfix | Wann |
|--------|------|
| `feat/` | neues Feature |
| `fix/` | Bug-Fix |
| `refactor/` | Umbau ohne neue Funktion |
| `docs/` | nur Dokumentation |
| `chore/` | Deps, Config, Migrations |

### Commit → Push → Deploy

```bash
# 1. Committen (pre-commit Hook prüft Syntax)
git add pollers/signal_engine.py
git commit -m "feat(signal): LLM Kommentar in Telegram-Alert"

# 2. GitHub Push
git push origin feat/llm-telegram-alerts

# 3. Deploy auf Devbox (NICHT scp!)
bash scripts/deploy.sh
```

**Kein scp ohne vorherigen Commit. Kein Deploy ohne sauberen `git status`.**

---

## Grafana — Workflow (Pflicht)

Grafana hat `updateIntervalSeconds: 30` im Provisioning. Jede Änderung an der lokalen JSON-Datei überschreibt manuelle UI-Änderungen nach max. 30 Sekunden.

**Vor jeder Dashboard-Änderung:**

```bash
bash scripts/grafana-sync.sh   # holt aktuellen Stand von API → überschreibt lokale Datei
```

Danach erst editieren, dann committen, dann:

```bash
bash scripts/grafana-push.sh   # API POST + scp gleichzeitig
```

**Nie die lokale JSON-Datei blind editieren und pushen.**

---

## Deploy-Prozess (scripts/deploy.sh)

Das Skript prüft vor jedem Deploy:
1. Keine uncommitted Änderungen im Repo
2. Python-Syntax aller Poller valide
3. Ziel-Branch ist nicht main (außer Hotfix)

```bash
bash scripts/deploy.sh              # normaler Deploy
bash scripts/deploy.sh --hotfix     # überspringt Branch-Check
```

---

## Devbox als Git-Remote

```bash
# Deploy = push zum Devbox-Remote → post-receive Hook startet Container neu
git push devbox feat/llm-telegram-alerts

# Oder nach Merge auf main:
git push devbox main
```

Der post-receive Hook auf der Devbox:
1. Checkt den Branch in `/home/hess/mount-midas/` aus
2. Führt `docker compose up --build -d` aus
3. Loggt Ergebnis nach `/home/hess/mount-midas/deploy.log`

---

## Was im Repo lebt (und was nicht)

| Im Repo | Nicht im Repo |
|---------|---------------|
| Alle Python-Poller | `.env` (Secrets) |
| Grafana JSON-Dashboards | `__pycache__/` |
| SQL-Migrations | |
| `docker-compose.yml` | |
| `scripts/` | |
| `docs/` (Backlog, Architektur) | |

`.env` wird aus `.env.example` abgeleitet — Secrets niemals committen.

---

## Grafana — Passwort-Management

**Problem:** Grafana speichert das Passwort im Docker-Volume (`mount_midas_grafana_data`), nicht in der `.env`. Die `.env`-Variable `GF_SECURITY_ADMIN_PASSWORD` gilt **nur beim ersten Container-Start**. Danach lebt das Passwort im Volume — wird es in der UI geändert, driftet es vom `.env`-Wert ab.

**Symptom:** API gibt `401 Invalid username or password` zurück, obwohl `.env` korrekt aussieht.

**Reset (wenn API 401 gibt):**

```bash
ssh hess@192.168.10.137
docker exec mount-midas-grafana /usr/share/grafana/bin/grafana cli admin reset-admin-password 'MidasGold2026!'
```

Das setzt das Passwort direkt im Volume zurück auf den `.env`-Wert.

**Regel:** Passwort in der UI nie ändern. Nur `.env` ist die Quelle der Wahrheit.

---

## Vor jeder Session: Sync-Check

```bash
bash scripts/sync-check.sh
```

Zeigt: lokaler Stand vs. Devbox, ob Container laufen, ob Grafana erreichbar ist.

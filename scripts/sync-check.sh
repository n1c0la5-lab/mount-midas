#!/usr/bin/env bash
# Session-Start Check: Devbox-Status, Git-Stand, Container, Grafana.
# Vor jeder Session ausführen.
set -euo pipefail

DEVBOX="hess@192.168.10.137"
GRAFANA="http://192.168.10.137:3000"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Mount Midas — Session-Start Check ==="
echo ""

# ── Git ──────────────────────────────────────────────────────
echo "--- Git ---"
BRANCH=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)
echo "Branch: $BRANCH"
if git -C "$REPO_DIR" diff --quiet && git -C "$REPO_DIR" diff --cached --quiet; then
  echo "✅ Working Tree: sauber"
else
  echo "⚠️  Working Tree: uncommitted Änderungen"
  git -C "$REPO_DIR" status --short
fi
LAST_COMMIT=$(git -C "$REPO_DIR" log --oneline -1)
echo "Letzter Commit: $LAST_COMMIT"

# ── Devbox Container ─────────────────────────────────────────
echo ""
echo "--- Devbox Container ---"
ssh "$DEVBOX" "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'mount-midas|NAMES'" 2>/dev/null || echo "❌ SSH zur Devbox fehlgeschlagen"

# ── Devbox vs. Repo: signal_engine.py Vergleich ──────────────
echo ""
echo "--- Datei-Sync Check ---"
LOCAL_MD5=$(md5sum "$REPO_DIR/pollers/signal_engine.py" | awk '{print $1}')
DEVBOX_MD5=$(ssh "$DEVBOX" "md5sum /home/hess/mount-midas/pollers/signal_engine.py 2>/dev/null | awk '{print \$1}'" 2>/dev/null || echo "error")
CONTAINER_MD5=$(ssh "$DEVBOX" "docker exec mount-midas-pollers md5sum /app/signal_engine.py 2>/dev/null | awk '{print \$1}'" 2>/dev/null || echo "error")

if [[ "$LOCAL_MD5" == "$DEVBOX_MD5" && "$LOCAL_MD5" == "$CONTAINER_MD5" ]]; then
  echo "✅ signal_engine.py: Repo = Devbox = Container"
else
  echo "⚠️  signal_engine.py DIVERGENZ:"
  echo "   Repo:      $LOCAL_MD5"
  echo "   Devbox:    $DEVBOX_MD5"
  echo "   Container: $CONTAINER_MD5"
fi

# ── Grafana erreichbar ────────────────────────────────────────
echo ""
echo "--- Grafana ---"
GF_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$GRAFANA/api/health" 2>/dev/null || echo "000")
if [[ "$GF_STATUS" == "200" ]]; then
  echo "✅ Grafana: erreichbar ($GRAFANA)"
else
  echo "❌ Grafana: nicht erreichbar (HTTP $GF_STATUS)"
fi

# ── Ollama ────────────────────────────────────────────────────
echo ""
echo "--- Ollama ---"
OLLAMA_STATUS=$(ssh "$DEVBOX" "curl -s -o /dev/null -w '%{http_code}' http://localhost:11434/api/tags" 2>/dev/null || echo "000")
if [[ "$OLLAMA_STATUS" == "200" ]]; then
  echo "✅ Ollama: läuft"
else
  echo "❌ Ollama: nicht erreichbar"
fi

echo ""
echo "=== Check abgeschlossen ==="

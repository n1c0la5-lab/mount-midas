#!/usr/bin/env bash
# Mount Midas — Deploy-Script
# Prüft den Zustand vor jedem Deploy. Kein scp mehr.
set -euo pipefail

DEVBOX="hess@192.168.10.137"
DEVBOX_DIR="/home/hess/mount-midas"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH=$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)
HOTFIX=${1:-}

echo "=== Mount Midas Deploy ==="
echo "Branch: $BRANCH"
echo ""

# ── Gate 1: Keine uncommitted Änderungen ──────────────────────
if ! git -C "$REPO_DIR" diff --quiet || ! git -C "$REPO_DIR" diff --cached --quiet; then
  echo "❌ STOP: Uncommitted Änderungen im Repo."
  echo "   git status:"
  git -C "$REPO_DIR" status --short
  echo ""
  echo "   Erst committen, dann deployen."
  exit 1
fi
echo "✅ Git: sauber"

# ── Gate 2: Nicht direkt auf main (außer --hotfix) ────────────
if [[ "$BRANCH" == "main" && "$HOTFIX" != "--hotfix" ]]; then
  echo "❌ STOP: Du bist auf main. Erst Feature-Branch erstellen."
  echo "   git checkout -b feat/dein-feature"
  echo "   Oder: bash scripts/deploy.sh --hotfix (nur für echte Notfälle)"
  exit 1
fi
if [[ "$BRANCH" == "main" && "$HOTFIX" == "--hotfix" ]]; then
  echo "⚠️  HOTFIX-Modus: deploy direkt von main"
fi

# ── Gate 3: Python-Syntax prüfen ─────────────────────────────
echo ""
echo "--- Python Syntax Check ---"
ERRORS=0
for f in "$REPO_DIR"/pollers/*.py; do
  if ! python3 -m py_compile "$f" 2>/dev/null; then
    echo "❌ Syntax-Fehler: $f"
    python3 -m py_compile "$f"
    ERRORS=$((ERRORS+1))
  fi
done
if [[ $ERRORS -gt 0 ]]; then
  echo "❌ STOP: $ERRORS Python-Datei(en) mit Syntax-Fehlern."
  exit 1
fi
echo "✅ Syntax: alle Poller valide"

# ── Deploy ────────────────────────────────────────────────────
echo ""
echo "--- Deploy auf Devbox ---"

# Poller-Dateien sync (nur pollers/ + docker-compose + migrations)
echo "→ sync pollers/..."
rsync -av --exclude='__pycache__' \
  "$REPO_DIR/pollers/" "$DEVBOX:$DEVBOX_DIR/pollers/"

echo "→ sync migrations/..."
rsync -av "$REPO_DIR/migrations/" "$DEVBOX:$DEVBOX_DIR/migrations/"

echo "→ sync docker-compose.yml..."
scp "$REPO_DIR/docker-compose.yml" "$DEVBOX:$DEVBOX_DIR/docker-compose.yml"

# Container neu bauen und starten
echo "→ docker compose rebuild..."
ssh "$DEVBOX" "cd $DEVBOX_DIR && docker compose up --build -d 2>&1 | tail -5"

echo ""
echo "✅ Deploy abgeschlossen: $BRANCH → Devbox"
echo "   Logs: ssh $DEVBOX 'docker logs -f mount-midas-pollers'"

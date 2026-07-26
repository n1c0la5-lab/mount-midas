#!/usr/bin/env bash
# Mount Midas — Migrationen auf die LIVE-DB anwenden
#
# Warum dieses Skript existiert:
# docker-compose mountet ./migrations nach /docker-entrypoint-initdb.d. Postgres
# führt dieses Verzeichnis NUR aus, wenn das Datenverzeichnis leer ist — also
# genau einmal, beim ersten Anlegen des Volumes. Bei einer laufenden Datenbank
# passiert nichts. `docker compose up --build -d` (und damit auch der
# post-receive-Hook) rollt also neuen Code aus, ohne das Schema mitzuziehen.
#
# Das ist keine Theorie: MM-10 hätte so den neuen dre_metrics-Code ohne
# np_reward_mints.fetched_at deployt und den Poller beim nächsten 04:30-Lauf
# zerbrochen.
#
# Alle Migrationen sind idempotent geschrieben (CREATE TABLE/INDEX IF NOT EXISTS,
# ADD COLUMN IF NOT EXISTS). Alle in Reihenfolge durchlaufen zu lassen ist daher
# sicher und für bereits angewendete Migrationen ein No-op. Genau das prüft auch
# das Gate gegen eine Wegwerf-DB.
#
#   bash scripts/migrate.sh                 # auf die Live-DB anwenden
#   bash scripts/migrate.sh --dry-run       # nur zeigen, was laufen würde
#   MM_DB=mm_probe bash scripts/migrate.sh  # gegen eine andere DB (zum Testen)
#
# MM_DB gibt es, damit dieser Schreibpfad überhaupt geprüft werden kann, ohne die
# Live-DB anzufassen — ein Skript, das nur scharf läuft, ist ungetestet.
set -uo pipefail

DEVBOX="hess@192.168.10.137"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DRY_RUN=${1:-}
DB=${MM_DB:-mount_midas}

echo "=== Mount Midas — Migrationen anwenden (DB: $DB) ==="
echo ""

FILES=$(cd "$REPO_DIR/migrations" && ls -1 *.sql | sort)
COUNT=$(echo "$FILES" | wc -l)
echo "$COUNT Migrationen im Repo:"
echo "$FILES" | sed 's/^/     /'
echo ""

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "--dry-run: nichts angewendet."
  echo ""
  echo "ACHTUNG: Ein Trockenlauf beweist hier nichts über den Schreibpfad."
  echo "Er listet nur Dateien. Der echte Beweis ist scripts/gate.sh, das die"
  echo "Migrationen gegen eine leere Wegwerf-DB durchlaufen lässt."
  exit 0
fi

# ── Übertragen ────────────────────────────────────────────────────────────────
if ! tar cf - -C "$REPO_DIR" migrations \
  | ssh "$DEVBOX" "rm -rf /tmp/mm-migrate && mkdir -p /tmp/mm-migrate \
      && tar xf - -C /tmp/mm-migrate \
      && docker exec mount-midas-db sh -c 'rm -rf /tmp/mig && mkdir -p /tmp/mig'"; then
  echo "❌ STOP: Übertragung fehlgeschlagen."
  exit 1
fi
ssh "$DEVBOX" 'for f in /tmp/mm-migrate/migrations/*.sql; do
    docker cp "$f" mount-midas-db:/tmp/mig/ >/dev/null || exit 1
  done' || { echo "❌ STOP: docker cp fehlgeschlagen."; exit 1; }

# ── Anwenden ──────────────────────────────────────────────────────────────────
echo "--- Anwenden auf $DB ---"
ssh "$DEVBOX" "DB='$DB' bash -s" <<'EOS'
FAIL=0
for f in $(docker exec mount-midas-db sh -c 'ls /tmp/mig/*.sql | sort'); do
  NAME=$(basename "$f")
  if OUT=$(docker exec mount-midas-db psql -U mount_midas -d "$DB" \
             -v ON_ERROR_STOP=1 -q -f "$f" 2>&1); then
    echo "✅ $NAME"
  else
    echo "❌ $NAME"
    echo "$OUT" | head -5 | sed 's/^/     /'
    FAIL=$((FAIL+1))
  fi
done
docker exec mount-midas-db sh -c 'rm -rf /tmp/mig'
rm -rf /tmp/mm-migrate
exit $FAIL
EOS
RC=$?

echo ""
if [[ $RC -ne 0 ]]; then
  echo "=== ❌ $RC Migration(en) fehlgeschlagen ==="
  echo "   Die fehlgeschlagene Migration in der Datei selbst korrigieren,"
  echo "   nicht per Hand an der DB nacharbeiten."
  exit 1
fi
echo "=== ✅ Alle $COUNT Migrationen angewendet ==="
echo "   Gegenprüfung: bash scripts/gate.sh (Schema-Drift muss null sein)"

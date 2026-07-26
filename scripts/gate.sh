#!/usr/bin/env bash
# Mount Midas — MM-10 Gate (vollständig, mit Datenbank)
#
# Läuft den regression_check gegen eine WEGWERF-Datenbank auf der Devbox und
# vergleicht anschließend das aus den Migrationen gebaute Schema mit dem
# Live-Schema. Live-Daten werden nie angefasst.
#
# Warum über die Devbox: psycopg ist lokal nicht installiert, im
# Poller-Container schon. Der Check läuft damit gegen dieselbe
# psycopg/Postgres-Version wie die Produktion.
#
#   bash scripts/gate.sh
set -uo pipefail

DEVBOX="hess@192.168.10.137"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TESTDB="mm_gatetest"

echo "=== Mount Midas — MM-10 Gate ==="
echo ""

# ── Dateien in den Container ──────────────────────────────────────────────────
echo "--- Übertrage Repo-Stand auf die Devbox ---"
if ! tar cf - -C "$REPO_DIR" --exclude='__pycache__' pollers migrations scripts grafana \
  | ssh "$DEVBOX" "rm -rf /tmp/mm-gate && mkdir -p /tmp/mm-gate \
      && tar xf - -C /tmp/mm-gate \
      && docker exec mount-midas-pollers rm -rf /tmp/mm-gate \
      && docker cp /tmp/mm-gate mount-midas-pollers:/tmp/mm-gate >/dev/null"; then
  echo "❌ STOP: Übertragung auf die Devbox fehlgeschlagen."
  exit 1
fi
echo "✅ übertragen"
echo ""

# ── Regression-Check gegen Wegwerf-DB ─────────────────────────────────────────
echo "--- Regression-Check ---"
ssh "$DEVBOX" "bash -s" <<EOS
set -u
docker exec mount-midas-db psql -U mount_midas -d postgres -q \
  -c "DROP DATABASE IF EXISTS $TESTDB;" -c "CREATE DATABASE $TESTDB;" 2>/dev/null
docker exec mount-midas-pollers sh -c '
  DSN="postgresql://\$DB_USER:\$DB_PASSWORD@\$DB_HOST:5432/$TESTDB"
  cd /tmp/mm-gate && python3 scripts/regression_check.py --dsn "\$DSN"
'
EOS
CHECK_RC=$?
echo ""

# ── Schema-Drift: Migrationen ⇄ Live ─────────────────────────────────────────
# Die goldene Regel des Repos ("nichts lebt nur auf der Devbox") ist ohne
# diesen Vergleich unbeweisbar. Zwei Spalten hatten sich so eingeschlichen.
echo "--- Schema-Drift: Migrationen ⇄ Live-DB ---"
DRIFT=$(ssh "$DEVBOX" "bash -s" <<'EOS'
cat > /tmp/schema_q.sql <<'SQL'
SELECT table_name || '.' || column_name || ' :: ' || data_type
FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1;
SQL
docker exec -i mount-midas-db psql -U mount_midas -d mount_midas -t -A -f /dev/stdin < /tmp/schema_q.sql | sort > /tmp/live.txt
docker exec -i mount-midas-db psql -U mount_midas -d mm_gatetest -t -A -f /dev/stdin < /tmp/schema_q.sql | sort > /tmp/mig.txt
comm -23 /tmp/live.txt /tmp/mig.txt
EOS
)

if [[ -n "$DRIFT" ]]; then
  echo "❌ Live-DB hat Spalten, die in keiner Migration stehen:"
  echo "$DRIFT" | sed 's/^/     /'
  echo ""
  echo "   Ein Frisch-Setup aus den Migrationen wäre unvollständig."
  echo "   Nachtragen (ADD COLUMN IF NOT EXISTS) in der neuesten Migration."
  CHECK_RC=1
else
  echo "✅ kein Drift — die Migrationen bauen das Live-Schema vollständig nach"
fi

# Hinweis, kein Fehler: die andere Richtung sind die noch nicht eingespielten
# Migrationen des aktuellen Branches. Das ist der Normalzustand vor dem Merge.
NEU=$(ssh "$DEVBOX" "comm -13 /tmp/live.txt /tmp/mig.txt" 2>/dev/null)
if [[ -n "$NEU" ]]; then
  echo ""
  echo "ℹ️  Noch nicht auf der Live-DB (neue Migrationen dieses Branches):"
  echo "$NEU" | sed 's/^/     /'
fi

# ── Aufräumen ────────────────────────────────────────────────────────────────
ssh "$DEVBOX" "docker exec mount-midas-db psql -U mount_midas -d postgres -q \
  -c 'DROP DATABASE IF EXISTS $TESTDB;' 2>/dev/null; \
  rm -rf /tmp/mm-gate; docker exec mount-midas-pollers rm -rf /tmp/mm-gate" >/dev/null 2>&1

echo ""
if [[ $CHECK_RC -ne 0 ]]; then
  echo "=== ❌ Gate fehlgeschlagen ==="
  exit 1
fi
echo "=== ✅ Gate bestanden ==="

#!/usr/bin/env bash
# Mount Midas — Wächter-Schwellen aus Messung herleiten (MM-10)
#
# Führt schwellen_messen.py im Poller-Container gegen die LIVE-DB aus. Nur
# lesend: SELECT auf system_health, sonst nichts.
#
# Warum über den Container (wie scripts/gate.sh): psycopg ist lokal nicht
# installiert, und data_watchdog.SOURCES lässt sich nur importieren, wo die
# DB-Umgebungsvariablen gesetzt sind. Die Liste zu parsen statt zu importieren
# wäre stiller Betrug — bei jeder Umformatierung ginge lautlos die halbe Liste
# verloren, und die Tabelle sähe trotzdem vollständig aus.
#
#   bash scripts/schwellen_messen.sh                      # letzte 24h
#   bash scripts/schwellen_messen.sh --seit 2026-07-27T09:06:31+00:00
#   bash scripts/schwellen_messen.sh --faktor 2.5
#
# Das Skript ändert NICHTS. Es misst und schlägt vor; die Entscheidung und der
# Kommentar "woraus stammt diese Zahl" gehören von Hand in data_watchdog.py.
set -uo pipefail

DEVBOX="hess@192.168.10.137"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Mount Midas — Schwellen messen ==="
echo ""

if ! tar cf - -C "$REPO_DIR" --exclude='__pycache__' pollers scripts \
  | ssh "$DEVBOX" "rm -rf /tmp/mm-schwellen && mkdir -p /tmp/mm-schwellen \
      && tar xf - -C /tmp/mm-schwellen \
      && docker exec mount-midas-pollers rm -rf /tmp/mm-schwellen \
      && docker cp /tmp/mm-schwellen mount-midas-pollers:/tmp/mm-schwellen >/dev/null"; then
  echo "❌ STOP: Übertragung auf die Devbox fehlgeschlagen."
  exit 1
fi

ssh "$DEVBOX" "bash -s" <<EOS
docker exec mount-midas-pollers sh -c '
  DSN="postgresql://\$DB_USER:\$DB_PASSWORD@\$DB_HOST:5432/\$DB_NAME"
  cd /tmp/mm-schwellen && python3 scripts/schwellen_messen.py --dsn "\$DSN" $*
'
EOS
RC=$?

ssh "$DEVBOX" "rm -rf /tmp/mm-schwellen; docker exec mount-midas-pollers rm -rf /tmp/mm-schwellen" 2>/dev/null
exit $RC

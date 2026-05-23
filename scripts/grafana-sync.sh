#!/usr/bin/env bash
# Holt den aktuellen Grafana-Stand von der API → überschreibt lokale JSON-Dateien.
# IMMER zuerst ausführen, bevor Dashboard-Dateien editiert werden.
set -euo pipefail

GRAFANA="http://192.168.10.137:3000"
DASHBOARDS_DIR="$(cd "$(dirname "$0")/../grafana/dashboards" && pwd)"

# Credentials aus .env laden wenn vorhanden
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
if [[ -f "$ENV_FILE" ]]; then
  GRAFANA_USER=$(grep '^GRAFANA_USER=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
  GRAFANA_PASS=$(grep '^GRAFANA_PASSWORD=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
else
  GRAFANA_USER="admin"
  GRAFANA_PASS="MidasAdmin2026"
fi

echo "=== Grafana Sync (API → Lokal) ==="
echo "Quelle: $GRAFANA"
echo ""

# Alle Dashboard-UIDs aus den lokalen Dateien lesen
for local_file in "$DASHBOARDS_DIR"/*.json; do
  fname=$(basename "$local_file")
  uid=$(python3 -c "import json,sys; d=json.load(open('$local_file')); print(d.get('uid',''))" 2>/dev/null || echo "")

  if [[ -z "$uid" ]]; then
    echo "⚠️  $fname — keine UID, übersprungen"
    continue
  fi

  response=$(curl -s -o /tmp/gf_dash.json -w "%{http_code}" \
    -u "$GRAFANA_USER:$GRAFANA_PASS" \
    "$GRAFANA/api/dashboards/uid/$uid")

  if [[ "$response" != "200" ]]; then
    echo "❌ $fname (uid=$uid) — HTTP $response"
    continue
  fi

  # Nur den dashboard-Block extrahieren und speichern
  python3 -c "
import json, sys
data = json.load(open('/tmp/gf_dash.json'))
dash = data['dashboard']
with open('$local_file', 'w') as f:
    json.dump(dash, f, indent=2, ensure_ascii=False)
    f.write('\n')
print('✅ $fname (uid=$uid) — aktualisiert')
"
done

echo ""
echo "Lokale Dateien sind jetzt auf dem Stand der Grafana-API."
echo "Jetzt erst editieren, dann: bash scripts/grafana-push.sh"

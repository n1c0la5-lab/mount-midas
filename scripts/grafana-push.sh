#!/usr/bin/env bash
# Pusht lokale Dashboard-JSONs gleichzeitig via API + scp (Provisioning-Datei).
# Schritt 2 nach grafana-sync.sh → editieren → grafana-push.sh
set -euo pipefail

GRAFANA="http://192.168.10.137:3000"
DEVBOX="hess@192.168.10.137"
DASHBOARDS_DIR="$(cd "$(dirname "$0")/../grafana/dashboards" && pwd)"
DEVBOX_DASH_DIR="/home/hess/mount-midas/grafana/dashboards"

ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
if [[ -f "$ENV_FILE" ]]; then
  GRAFANA_USER=$(grep '^GRAFANA_USER=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
  GRAFANA_PASS=$(grep '^GRAFANA_PASSWORD=' "$ENV_FILE" | cut -d= -f2 | tr -d '"')
else
  GRAFANA_USER="admin"
  GRAFANA_PASS="MidasAdmin2026"
fi

DASHBOARD_FILE=${1:-}

if [[ -z "$DASHBOARD_FILE" ]]; then
  echo "Usage: bash scripts/grafana-push.sh grafana/dashboards/mount_midas_main.json"
  exit 1
fi

if [[ ! -f "$DASHBOARD_FILE" ]]; then
  echo "❌ Datei nicht gefunden: $DASHBOARD_FILE"
  exit 1
fi

fname=$(basename "$DASHBOARD_FILE")
echo "=== Grafana Push: $fname ==="

# Payload bauen
python3 -c "
import json, sys
dash = json.load(open('$DASHBOARD_FILE'))
payload = {'dashboard': dash, 'overwrite': True, 'folderId': 0}
with open('/tmp/gf_push_payload.json', 'w') as f:
    json.dump(payload, f)
print('Payload gebaut: ' + str(len(json.dumps(payload))) + ' Bytes')
"

# Gleichzeitig: API POST + scp
echo "→ API POST..."
HTTP=$(curl -s -o /tmp/gf_push_result.json -w "%{http_code}" \
  -X POST \
  -H "Content-Type: application/json" \
  -u "$GRAFANA_USER:$GRAFANA_PASS" \
  -d @/tmp/gf_push_payload.json \
  "$GRAFANA/api/dashboards/db")

echo "→ scp Provisioning-Datei..."
scp "$DASHBOARD_FILE" "$DEVBOX:$DEVBOX_DASH_DIR/$fname"

if [[ "$HTTP" == "200" ]]; then
  echo "✅ API: HTTP $HTTP"
else
  echo "❌ API: HTTP $HTTP"
  cat /tmp/gf_push_result.json
  exit 1
fi

echo "✅ Push abgeschlossen: API + Provisioning-Datei aktualisiert"

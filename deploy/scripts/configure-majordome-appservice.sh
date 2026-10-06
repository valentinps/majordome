#!/bin/bash
# Registers Majordome with Synapse as a *passive* appservice.
#
# Non-exclusive namespaces over the bridge bots and ghosts: Synapse pushes every event of every
# room those users are in, so Majordome never has to join rooms as the user (joining makes
# bridges send read receipts). Usage: run from the deploy directory on the server.
set -euo pipefail
cd "$(dirname "$0")/.."

for k in MAJORDOME_AS_TOKEN MAJORDOME_HS_TOKEN; do
  grep -q "^$k=" .env || echo "$k=$(openssl rand -hex 32)" >> .env
done
source .env
DOMAIN_RE="${SERVER_NAME//./\\.}"

cat > data/synapse/appservices/majordome.yaml <<YAML
id: majordome
url: http://majordome-worker:8009
as_token: "${MAJORDOME_AS_TOKEN}"
hs_token: "${MAJORDOME_HS_TOKEN}"
sender_localpart: majordome
rate_limited: false
receive_ephemeral: false
namespaces:
  users:
    - { exclusive: false, regex: "@whatsappbot:${DOMAIN_RE}" }
    - { exclusive: false, regex: "@whatsapp_.*:${DOMAIN_RE}" }
    - { exclusive: false, regex: "@discordbot:${DOMAIN_RE}" }
    - { exclusive: false, regex: "@discord_.*:${DOMAIN_RE}" }
  aliases: []
  rooms: []
YAML
chmod 640 data/synapse/appservices/majordome.yaml
./scripts/configure-synapse.sh
chown -R 991:991 data/synapse
echo "Majordome appservice registered. Restart synapse and majordome-worker."

#!/usr/bin/env bash
# Provision the teleop benchmark VM on GCP. Requires: gcloud auth login done.
# Usage: ./provision_gcp.sh [zone]   (default us-west1-b)
set -euo pipefail

PROJECT=innate-managed-infra
ZONE="${1:-us-west1-b}"
NAME=teleop-bench-1
DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "== creating VM $NAME in $ZONE =="
gcloud compute instances create "$NAME" \
  --project="$PROJECT" --zone="$ZONE" \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB --tags=teleop-bench

echo "== firewall rules =="
gcloud compute firewall-rules create teleop-bench-tcp \
  --project="$PROJECT" --target-tags=teleop-bench --allow \
  tcp:3478,tcp:5349,tcp:7447,tcp:7880,tcp:7881,tcp:8765,tcp:5201 \
  2>/dev/null || echo "  (tcp rule exists)"
gcloud compute firewall-rules create teleop-bench-udp \
  --project="$PROJECT" --target-tags=teleop-bench --allow \
  udp:3478,udp:7882,udp:9998,udp:5201,udp:49160-49960,udp:50000-60000 \
  2>/dev/null || echo "  (udp rule exists)"

IP=$(gcloud compute instances describe "$NAME" --project="$PROJECT" \
  --zone="$ZONE" --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
echo "== VM external IP: $IP =="

echo "== waiting for SSH =="
for i in $(seq 1 30); do
  gcloud compute ssh "$NAME" --project="$PROJECT" --zone="$ZONE" \
    --command="echo ssh-ok" 2>/dev/null && break
  sleep 5
done

echo "== copying files + running setup =="
gcloud compute scp --project="$PROJECT" --zone="$ZONE" \
  "$DIR/relay_server.py" "$DIR/common.py" "$DIR/deploy/setup_vm.sh" "$NAME":~
gcloud compute ssh "$NAME" --project="$PROJECT" --zone="$ZONE" \
  --command="chmod +x ~/setup_vm.sh && ~/setup_vm.sh $IP"

echo ""
echo "READY. Endpoints:"
echo "  WS relay + inference sim : ws://$IP:8765"
echo "  TURN (coturn)            : turn:$IP:3478 (user bench / pass benchpw)"
echo "  LiveKit                  : ws://$IP:7880 (devkey / secret)"
echo "  Zenoh router             : tcp/$IP:7447"
echo "  UDP echo (RTT floor)     : $IP:9998"

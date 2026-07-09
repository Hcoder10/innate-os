#!/usr/bin/env bash
# Runs ON the GCP VM. Installs docker + services:
#   coturn (TURN), livekit-server (dev mode), zenoh router,
#   python relay_server (WS relay + inference sim), udp echo.
# Usage: ./setup_vm.sh <EXTERNAL_IP>
set -euo pipefail

EXT_IP="${1:?usage: setup_vm.sh <external-ip>}"

if ! command -v docker >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker.io python3-venv iperf3
  sudo systemctl enable --now docker
fi

sudo docker rm -f coturn livekit zenohd 2>/dev/null || true

echo "== coturn =="
sudo docker run -d --network=host --name coturn --restart=unless-stopped \
  coturn/coturn \
  -n --log-file=stdout --lt-cred-mech --user=bench:benchpw --realm=bench \
  --listening-port=3478 --min-port=49160 --max-port=49960 \
  --external-ip="$EXT_IP"

echo "== livekit (dev mode: devkey/secret) =="
sudo docker run -d --network=host --name livekit --restart=unless-stopped \
  livekit/livekit-server --dev --bind 0.0.0.0 --node-ip "$EXT_IP"

echo "== zenoh router =="
sudo docker run -d --network=host --name zenohd --restart=unless-stopped \
  eclipse/zenoh:1.5.1 --listen tcp/0.0.0.0:7447

echo "== relay server (WS relay + inference sim) =="
python3 -m venv ~/venv 2>/dev/null || true
~/venv/bin/pip install -q "websockets>=12"
cat | sudo tee /etc/systemd/system/teleop-relay.service >/dev/null <<EOF
[Unit]
Description=teleop bench relay
After=network.target
[Service]
ExecStart=/home/$USER/venv/bin/python /home/$USER/relay_server.py
Restart=always
User=$USER
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now teleop-relay

echo "== udp echo on :9998 (raw internet RTT floor) =="
cat | sudo tee /etc/systemd/system/udp-echo.service >/dev/null <<'EOF'
[Unit]
Description=udp echo
After=network.target
[Service]
ExecStart=/usr/bin/python3 -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('0.0.0.0',9998));exec('while True:\n d,a=s.recvfrom(2048)\n s.sendto(d,a)')"
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now udp-echo

echo "== status =="
sudo docker ps --format '{{.Names}}  {{.Status}}'
systemctl is-active teleop-relay udp-echo
echo "setup complete on $EXT_IP"

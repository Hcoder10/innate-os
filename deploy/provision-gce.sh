#!/bin/bash
# Provision a fresh Debian GCE instance with the system packages the simulator
# needs before `./innate setup` and `./innate sim up` will succeed.
#
# These are host-level dependencies that live outside the repo, so they are not
# installed by sim/setup.sh. Re-run is safe (apt is idempotent).
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]] && ! sudo -n true 2>/dev/null; then
	echo "This script needs sudo (passwordless or run as root)." >&2
	exit 1
fi
SUDO=""; [[ "$(id -u)" -ne 0 ]] && SUDO="sudo"

export DEBIAN_FRONTEND=noninteractive

echo "==> Node 22 + corepack (yarn) — frontend build"
curl -fsSL https://deb.nodesource.com/setup_22.x | $SUDO -E bash -
$SUDO apt-get install -y nodejs
$SUDO corepack enable

echo "==> Docker engine — asset pull + OS dev container"
# The instance may ship only docker-cli; docker.io provides the daemon + containerd.
$SUDO apt-get install -y docker.io
$SUDO systemctl enable --now docker
# Let the invoking (non-root) user talk to the daemon; re-login to pick up the group.
if [[ -n "${SUDO_USER:-}" ]]; then $SUDO usermod -aG docker "$SUDO_USER" || true; fi

echo "==> git-lfs — ReplicaCAD scene data is LFS-backed"
$SUDO apt-get install -y git-lfs
git lfs install

echo "==> OpenCV runtime libs (cv2 needs libGL/libglib)"
$SUDO apt-get install -y libgl1 libglib2.0-0t64

echo "==> Headless EGL stack — Genesis renders offscreen (mesa software EGL, no GPU)"
$SUDO apt-get install -y libegl1 libegl-mesa0 libgl1-mesa-dri libgles2 libopengl0

echo "==> Caddy — TLS reverse proxy for the public demo (optional)"
$SUDO apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
	| $SUDO gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
	| $SUDO tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
$SUDO apt-get update
$SUDO apt-get install -y caddy

echo
echo "Done. Next:"
echo "  1. (new shell so the docker group applies)"
echo "  2. cd <repo> && ./innate setup && ./innate sim up"
echo "  3. To expose publicly, see deploy/README.md (Caddy + frontend .env)."

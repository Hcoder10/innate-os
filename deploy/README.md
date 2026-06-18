# Running the simulator on a GCE instance

Notes for standing up `./innate sim up` on a fresh Debian GCE VM and (optionally)
exposing the dashboard publicly behind Caddy. The in-repo build fix
(`sim/setup.sh`) handles the netifaces compile; everything here is host-level
setup that lives outside the repo.

## 1. System dependencies

```bash
sudo deploy/provision-gce.sh
```

Installs: Node 22 + yarn (frontend build), the Docker engine (`docker.io` — many
images ship only `docker-cli`), git-lfs (ReplicaCAD scene data), the OpenCV
runtime libs (`libgl1`, `libglib2.0-0t64`), the headless mesa **EGL** stack
(`libegl1 libegl-mesa0 libgl1-mesa-dri libgles2 libopengl0`, for Genesis
offscreen rendering on a GPU-less box), and Caddy.

Start a new shell afterward so your user picks up the `docker` group.

## 2. Build and run

```bash
./innate setup
./innate sim up
```

On a CPU-only instance Genesis renders via mesa/llvmpipe and physics runs slower
than real-time — the dashboard shows `Mood: DEGRADED`, which is expected.

## 3. Expose the dashboard publicly (Caddy)

The simulator backend serves the dashboard on `:8000` and rosbridge listens on
`:9090`. The frontend bakes its endpoint URLs at **build time** (Vite
`import.meta.env.VITE_*`), so they must point at the public origin *before* you
build, and rosbridge must be reachable over same-origin `wss` (the page is HTTPS,
so `ws://localhost:9090` is both wrong-host and blocked mixed-content).

**Prerequisites**

- DNS: an `A` record for the host (e.g. `sim-demo.innate.bot`) pointing at the
  instance's external IP, **DNS-only** (not behind a proxy) so Caddy can do
  ACME and terminate TLS itself.
- Firewall: the instance needs tags allowing 80/443 (`http-server,https-server`).

**Frontend env** — create `sim/frontend/.env` (gitignored), then rebuild:

```bash
cat > sim/frontend/.env <<'EOF'
VITE_SIM_BASE_URL=https://sim-demo.innate.bot
VITE_WS_BASE_URL=wss://sim-demo.innate.bot
VITE_DIRECT_ROBOT=false
VITE_ROBOT_WS_URL=wss://sim-demo.innate.bot/rosbridge
EOF
(cd sim/frontend && yarn build)
```

**Caddy** — install the proxy config and start it:

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
```

Verify: `curl -I https://sim-demo.innate.bot/` returns 200, and a WS upgrade to
`/rosbridge` returns `101 Switching Protocols`.

#!/usr/bin/env bash
# Container start: write runtime config, rebuild the web app (logging build state
# over HTTP so the launcher can read it), then serve with Caddy.
set -uo pipefail

WEB_DIR=/srv/web
STATUS_DIR=/srv/status
CONFIG_DIR=/srv/config
STATUS_FILE="$STATUS_DIR/build-status.json"
BUILD_LOG="$STATUS_DIR/build.log"

mkdir -p "$WEB_DIR" "$STATUS_DIR" "$CONFIG_DIR"

# 1. Runtime config.json from env (browser-facing URLs).
cat > "$CONFIG_DIR/config.json" <<EOF
{
  "simBaseUrl": "${SIM_BASE_URL:-http://localhost:8000}",
  "wsBaseUrl": "${WS_BASE_URL:-ws://localhost:8000}",
  "robotWsUrl": "${ROBOT_WS_URL:-ws://localhost:9090}",
  "directRobot": ${DIRECT_ROBOT:-false},
  "cartesiaApiKey": "${CARTESIA_API_KEY:-}",
  "pinnedSkills": ${PINNED_SKILLS:-[\"navigate with vision\", \"navigate with position\", \"wave\"]}
}
EOF

# 2. Seed build state + a self-refreshing placeholder page.
echo '{"state":"building"}' > "$STATUS_FILE"
: > "$BUILD_LOG"
cat > "$WEB_DIR/index.html" <<'EOF'
<!doctype html><meta charset="utf-8"><title>Building…</title>
<body style="font-family:monospace;padding:2rem">Building the web app… this page will refresh.
<script>setTimeout(function(){location.reload()},2000)</script></body>
EOF

# 3. Build in the background; swap dist into the web root on success, error page on failure.
#    Output is teed to build.log (served at /build.log) and to container stdout.
(
  echo "[entrypoint] starting web build"
  if yarn build 2>&1 | tee "$BUILD_LOG"; then
    rm -rf "${WEB_DIR:?}"/*
    cp -r dist/. "$WEB_DIR"/
    echo '{"state":"ready"}' > "$STATUS_FILE"
    echo "[entrypoint] web build succeeded"
  else
    echo '{"state":"error"}' > "$STATUS_FILE"
    {
      echo '<!doctype html><meta charset="utf-8"><title>Build failed</title>'
      echo '<body style="font-family:monospace;padding:2rem"><h1>Web build failed</h1><pre>'
      sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g' "$BUILD_LOG"
      echo '</pre></body>'
    } > "$WEB_DIR/index.html"
    echo "[entrypoint] web build FAILED"
  fi
) &

# 4. Serve in the foreground (PID 1). Caddy is up immediately, so build state is
#    readable while the background build runs.
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile

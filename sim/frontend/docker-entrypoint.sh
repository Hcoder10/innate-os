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

# 1. Runtime config.json: start from the shipped defaults (public/config.json,
#    the single source of truth) and overlay only the env vars that are set.
node -e '
  const fs = require("fs");
  const out = JSON.parse(fs.readFileSync("/app/public/config.json", "utf8"));
  const set = (key, env, parse = String) => {
    if (process.env[env] == null) return;
    try {
      out[key] = parse(process.env[env]);
    } catch (e) {
      // A malformed value (e.g. non-JSON PINNED_SKILLS) must not abort config
      // generation and leave the app with no runtime config; keep the default.
      console.error(`[entrypoint] ignoring invalid ${env}: ${e.message}`);
    }
  };
  set("simBaseUrl",     "SIM_BASE_URL");
  set("wsBaseUrl",      "WS_BASE_URL");
  set("robotWsUrl",     "ROBOT_WS_URL");
  set("directRobot",    "DIRECT_ROBOT",     v => v === "true");
  // cartesiaApiKey is intentionally NOT mapped here: config.json is served
  // unauthenticated at /config.json, so writing CARTESIA_API_KEY into it would
  // leak a billable key to anyone who can reach the frontend. Browser TTS is
  // not a feature we need, so we accept it being non-functional. Do not re-add.
  set("pinnedSkills",   "PINNED_SKILLS",    JSON.parse);
  fs.writeFileSync(process.argv[1], JSON.stringify(out));
' "$CONFIG_DIR/config.json"

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

#!/bin/bash
# Restore this robot to the "used robot" snapshot captured on this branch
# (sim/used-robot-fixture). One command:
#
#     ./scripts/restore_used_robot.sh
#
# What it does:
#   1. Syncs the fixture's user state into place (custom skills/agents, maps,
#      nav mode/map selection, patrol waypoints), deleting anything extra so the
#      result matches the snapshot exactly.
#   2. Restores the large recordings/checkpoints from a blob cache
#      (default /home/jetson1/robot-fixtures/used-robot-2026-08-08/blobs, or a
#      directory/URL given via BLOB_SOURCE), verifying sizes against
#      blobs.manifest.tsv (sha256 too with VERIFY=1).
#   3. Restarts ros-app.service and prints a verification summary.
#
# See sim/used-robot-fixture/README.md for what the snapshot contains.
set -euo pipefail

REPO="${INNATE_OS_ROOT:-/home/jetson1/innate-os}"
FIX="$REPO/sim/used-robot-fixture"
# Blob source: local hardlink cache when present (instant), else the public
# bucket the snapshot was published to. Override with BLOB_SOURCE=<dir-or-url>.
LOCAL_CACHE=/home/jetson1/robot-fixtures/used-robot-2026-08-08/blobs
GCS_URL=https://storage.googleapis.com/innate-robot-fixtures/used-robot-2026-08-08
if [ -z "${BLOB_SOURCE:-}" ]; then
    if [ -d "$LOCAL_CACHE" ]; then BLOB_SOURCE="$LOCAL_CACHE"; else BLOB_SOURCE="$GCS_URL"; fi
fi
MANIFEST="$FIX/blobs.manifest.tsv"

[ "$(id -u)" -eq 0 ] && { echo "Run as the robot user, not root." >&2; exit 1; }
[ -d "$FIX/state" ] || { echo "Fixture not found at $FIX — are you on the sim/used-robot-* branch?" >&2; exit 1; }

echo "== Restoring used-robot snapshot from $FIX"

# --- 1. small state (no --delete yet: blobs are restored in step 2, the
#        cleanup in step 3 removes anything that belongs to neither) ---
rsync -a "$FIX/state/innate-os/workspace/custom_agents/" "$REPO/workspace/custom_agents/"
rsync -a "$FIX/state/innate-os/workspace/custom_skills/" "$REPO/workspace/custom_skills/"
mkdir -p "$REPO/data"
rsync -a "$FIX/state/innate-os/data/maps/" "$REPO/data/maps/"
cp -a "$FIX/state/innate-os/data/.last_mode" "$FIX/state/innate-os/data/.last_map" "$REPO/data/"
cp -a "$FIX/state/home/patrol_waypoints.json" "$HOME/patrol_waypoints.json"

# --- 2. large blobs ---
fetch_blob() { # rel dest
    local rel="$1" dest="$2"
    mkdir -p "$(dirname "$dest")"
    case "$BLOB_SOURCE" in
        http://*|https://*) curl -fsSL -o "$dest" "$BLOB_SOURCE/$rel" ;;
        *)  [ -f "$BLOB_SOURCE/$rel" ] || { echo "  MISSING in blob source: $rel" >&2; return 1; }
            cp -f "$BLOB_SOURCE/$rel" "$dest" ;;
    esac
}

missing=0
while IFS=$'\t' read -r rel size sha; do
    dest="$REPO/$rel"
    have_size=$(stat -c%s "$dest" 2>/dev/null || echo -1)
    if [ "$have_size" != "$size" ]; then
        echo "  fetching blob: $rel"
        fetch_blob "$rel" "$dest" || { missing=$((missing + 1)); continue; }
    fi
    if [ "${VERIFY:-0}" = "1" ]; then
        have_sha=$(sha256sum "$dest" | cut -d' ' -f1)
        [ "$have_sha" = "$sha" ] || { echo "  CHECKSUM MISMATCH: $rel" >&2; missing=$((missing + 1)); }
    fi
done < "$MANIFEST"
[ "$missing" -gt 0 ] && echo "WARNING: $missing blob(s) could not be restored (set BLOB_SOURCE to a copy of the blob cache)" >&2

# --- 3. delete anything the snapshot doesn't contain ---
for top in custom_agents custom_skills; do
    ( cd "$REPO/workspace" && find "$top" -type f ) | while read -r rel; do
        in_fixture="$FIX/state/innate-os/workspace/$rel"
        if [ ! -f "$in_fixture" ] && ! grep -qF "workspace/$rel" <(cut -f1 "$MANIFEST"); then
            rm -f "$REPO/workspace/$rel"
            echo "  removed extraneous: workspace/$rel"
        fi
    done
    ( cd "$REPO/workspace" && find "$top" -depth -type d -empty -delete )
done

# --- .env sanity (the snapshot holds no secrets) ---
grep -q "^INNATE_SERVICE_KEY=." "$REPO/.env" 2>/dev/null || \
    echo "WARNING: INNATE_SERVICE_KEY missing from $REPO/.env — cloud brain will not connect" >&2

# --- 4. restart + verify ---
echo "== Restarting ros-app.service"
sudo -n /usr/bin/systemctl restart ros-app.service

echo "== Waiting for stack (up to 5 min)..."
if source /opt/ros/humble/setup.bash 2>/dev/null && \
   source "$REPO/ros2_ws/install/setup.bash" 2>/dev/null && \
   source "$REPO/config/dds/setup_dds.zsh" 2>/dev/null; then
    mode=""
    for _ in $(seq 1 60); do
        mode=$(timeout -k 3 8 ros2 topic echo /nav/current_mode --once 2>/dev/null | awk '/^data:/{print $2}' || true)
        if [ -n "$mode" ] && [ "$mode" != "switching" ]; then break; fi
        sleep 5
    done
    echo "   nav mode: ${mode:-<none>} (expected: navigation)"
    echo "   map:      $(cat "$REPO/data/.last_map") (expected: office_demo.yaml)"
    echo "   skills:"
    timeout -k 3 30 "$REPO/scripts/innate" skill list 2>/dev/null | grep -E "local/" || echo "   (skill list unavailable)"
else
    echo "   (ROS env unavailable — skipped live verification)"
fi

echo "== Done. Robot restored to the used-robot snapshot."

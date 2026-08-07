#!/usr/bin/env bash
# Decompose every room at the settings in decompose_rooms.py.
#
# All rooms at once, one CoACD process each. That needs a machine with real
# memory: each room peaks around 12.7 GB resident, measured, so five together
# want ~64 GB and a 32 GB runner gets OOM-killed partway through -- which is
# why CI runs this on `huge`.
#
# Do not use mesh size to predict the cost: CoACD's cost tracks concavity, so
# Sala_Cozinha (~104k verts) is among the CHEAPER rooms at 352s while Quartos
# (14.5k verts) is the most expensive at 640s.
#
# Output is TEE'd, never just redirected: an OOM-killed container takes every
# log file inside it along, so anything not already on stdout is gone. The
# files are kept too, for `tail -f` locally.
#
# bash, not sh: ${PIPESTATUS[0]} is how the decomposer's exit status survives
# the tee pipeline. Interrupting is safe -- a room's old hulls are only
# replaced once its decomposition finishes.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

ROOMS="Sala_Cozinha Banheiros_Corredor Chambre01_Meuble_Lit Quartos Teto"

trap 'kill 0' INT TERM  # Ctrl-C kills all room workers, not just the wait

status=0

bake() {
  room="$1"
  # awk rather than `sed -u`: the parallel rooms interleave on stdout, so each
  # line needs its room name, and BSD sed (macOS) has no -u to line-buffer with.
  uv run decompose_rooms.py "$room" 2>&1 \
    | awk -v r="$room" '{print "[" r "] " $0; fflush()}' \
    | tee "../assets/decomp400_$room.log"
  return "${PIPESTATUS[0]}"
}

echo "=== baking $(echo "$ROOMS" | wc -w | tr -d ' ') rooms in parallel ==="
pids=""
for room in $ROOMS; do
  bake "$room" &
  pids="$pids $!"
done
# Each child is waited on individually: a bare `wait` returns 0 even when a
# room died, which would ship an image whose hulls are silently incomplete.
for pid in $pids; do
  wait "$pid" || status=1
done

echo "=== all rooms done (status=$status) ==="
for room in $ROOMS; do
  printf '%s: %s\n' "$room" "$(tail -1 "../assets/decomp400_$room.log" 2>/dev/null || echo '(no log)')"
done
exit "$status"

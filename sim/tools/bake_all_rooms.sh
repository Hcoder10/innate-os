#!/bin/sh
# Decompose every room in parallel (one CoACD process each) at the settings
# in decompose_rooms.py. Logs: ../assets/decomp400_<room>.log. Interrupting is
# safe: a room's old hulls are only replaced after its decomposition finishes.
cd "$(dirname "$0")" || exit 1

ROOMS="Sala_Cozinha Banheiros_Corredor Chambre01_Meuble_Lit Quartos Teto"

for room in $ROOMS; do
  uv run decompose_rooms.py "$room" > "../assets/decomp400_$room.log" 2>&1 &
done

trap 'kill 0' INT TERM  # Ctrl-C kills all room workers, not just the wait

echo "baking $(echo "$ROOMS" | wc -w | tr -d ' ') rooms in parallel; tail -f ../assets/decomp400_<room>.log for progress"
wait
echo "=== all rooms done ==="
for room in $ROOMS; do
  printf '%s: %s\n' "$room" "$(tail -1 "../assets/decomp400_$room.log")"
done

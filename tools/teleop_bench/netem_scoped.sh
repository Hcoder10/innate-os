#!/usr/bin/env bash
# Scoped egress impairment on the robot: applies netem (delay/jitter/loss)
# ONLY to traffic destined for one peer IP, leaving SSH / ROS / everything
# else untouched. Egress-only (netem shapes what the robot sends).
#
# Usage:
#   sudo ./netem_scoped.sh apply <peer-ip> <delay-ms> <jitter-ms> <loss-pct> [dev]
#   sudo ./netem_scoped.sh clear [dev]
#   sudo ./netem_scoped.sh show  [dev]
#
# Example (simulate a 40ms/8ms-jitter/1%-loss WAN toward the operator):
#   sudo ./netem_scoped.sh apply 192.168.0.123 40 8 1
set -euo pipefail

CMD="${1:?apply|clear|show}"
DEV_DEFAULT=wlP1p1s0

case "$CMD" in
  apply)
    PEER="${2:?peer ip}"; DELAY="${3:?delay ms}"; JITTER="${4:?jitter ms}"
    LOSS="${5:?loss pct}"; DEV="${6:-$DEV_DEFAULT}"
    tc qdisc del dev "$DEV" root 2>/dev/null || true
    # band 3 of prio gets netem; only traffic to $PEER is filtered into it
    tc qdisc add dev "$DEV" root handle 1: prio bands 4 \
      priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1
    tc qdisc add dev "$DEV" parent 1:4 handle 40: netem \
      delay "${DELAY}ms" "${JITTER}ms" loss "${LOSS}%"
    tc filter add dev "$DEV" protocol ip parent 1:0 prio 1 u32 \
      match ip dst "$PEER"/32 flowid 1:4
    echo "netem ${DELAY}ms/${JITTER}ms/${LOSS}% applied on $DEV toward $PEER"
    ;;
  clear)
    DEV="${2:-$DEV_DEFAULT}"
    tc qdisc del dev "$DEV" root 2>/dev/null || true
    echo "cleared $DEV"
    ;;
  show)
    DEV="${2:-$DEV_DEFAULT}"
    tc qdisc show dev "$DEV"; tc filter show dev "$DEV"
    ;;
  *) echo "unknown cmd $CMD"; exit 1 ;;
esac

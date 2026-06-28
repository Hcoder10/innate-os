#!/bin/sh
# Redirect privileged port 443 -> 4443, the unprivileged port the webapp proxy
# binds (proxy/https_server.py runs as the jetson1 user and can't bind a port
# below 1024). With this in place the cockpit is reachable at
# https://<robot>.local with no ":4443" in the URL, while the proxy stays
# unprivileged.
#
# Uses iptables-legacy's REDIRECT target rather than nftables: the Jetson/L4T
# kernel does NOT ship the nftables `nat` chain type ("nft ... type nat hook
# prerouting" fails with ENOENT), but the classic iptable_nat path works.
# Idempotent: drop any existing copy of the rule before adding exactly one, so
# re-applying on boot / restart never stacks duplicates.
#
# Handles traffic arriving from other hosts (phones, laptops) via PREROUTING;
# reaching :443 from the robot itself (localhost) would need an OUTPUT rule too.
# IPv4 only, matching the proxy (it binds 0.0.0.0:4443).
set -e

# -w makes iptables wait for the xtables lock instead of failing when another
# netfilter caller holds it (races at boot / with concurrent invocations).
IPT="/usr/sbin/iptables -w"
RULE="PREROUTING -p tcp --dport 443 -j REDIRECT --to-ports 4443"

# Drop any existing copies first (idempotent), then add exactly one.
while $IPT -t nat -C $RULE 2>/dev/null; do
	$IPT -t nat -D $RULE
done
$IPT -t nat -A $RULE

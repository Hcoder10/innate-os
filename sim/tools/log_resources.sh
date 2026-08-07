#!/bin/sh
# Sample memory and CPU to stdout every N seconds (default 10).
#
# Exists because the asset bake gets OOM-killed rather than failing: dmesg is
# not reachable from a hosted runner, so CI shows a bare "Killed" with no clue
# how close to the edge it was or which stage was holding the memory.
#
# OFF unless SIM_RESOURCE_LOG=1 -- the caller checks, not this script.
#
# /proc and /sys read directly: this runs inside python:3.12-slim, which has
# neither `free` nor `top` (procps is not installed).
#
# Usage: tools/log_resources.sh [interval_seconds]
interval="${1:-10}"

# cgroup v2 reports what the CONTAINER may use; /proc/meminfo reports the host,
# which is the wrong number the moment a memory limit is set. Prefer the former.
read_mem_mb() {
  if [ -r /sys/fs/cgroup/memory.current ]; then
    cur=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)
    max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo max)
    [ "$max" = "max" ] && max=$(awk '/MemTotal/ {print $2 * 1024}' /proc/meminfo)
    echo "$cur $max" | awk '{printf "%.1f %.1f", $1/1048576, $2/1048576}'
  else
    awk '/MemTotal/ {t=$2} /MemAvailable/ {a=$2} END {printf "%.1f %.1f", (t-a)/1024, t/1024}' /proc/meminfo
  fi
}

# The biggest resident processes, which is what the OOM killer scores on -- so
# this names the likely victim before it becomes one.
top_rss() {
  for s in /proc/[0-9]*/status; do
    [ -r "$s" ] || continue
    awk '/^Name:/ {n=$2} /^VmRSS:/ {r=$2} END {if (r > 0) printf "%d %s\n", r, n}' "$s" 2>/dev/null
  done | sort -rn | head -3 | awk '{printf "%s=%.1fMB ", $2, $1/1024}'
}

echo "[resources] sampling every ${interval}s (cores=$(nproc 2>/dev/null || echo '?'))"
while :; do
  set -- $(read_mem_mb)
  used="$1"; total="$2"
  pct=$(awk -v u="$used" -v t="$total" 'BEGIN {printf "%.0f", (t > 0 ? u * 100 / t : 0)}')
  load=$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo '?')
  echo "[resources] mem ${used}/${total} MB (${pct}%)  load1 ${load}  top: $(top_rss)"
  sleep "$interval"
done

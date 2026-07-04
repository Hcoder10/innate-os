#!/bin/bash
# This script is intended to be run via sudo by the BLE provisioning service
# when a network change requires restarting ROS/DDS components.

echo "Restarting Zenoh Server due to network change..." >&2
systemctl restart zenoh-router.service
if [ $? -ne 0 ]; then
  echo "ERROR: Failed to restart zenoh-router.service" >&2
  # Optionally exit here if discovery server restart is critical
  # exit 1
fi

echo "Restarting ROS Application due to network change..." >&2
systemctl restart ros-app.service
if [ $? -ne 0 ]; then
  echo "ERROR: Failed to restart ros-app.service" >&2
  # Optionally exit here if ROS app restart is critical
  # exit 1
fi

echo "Service restarts requested." >&2
INNATE_OS_ROOT="${INNATE_OS_ROOT:-/home/jetson1/innate-os}"
WEBAPP_URI_SCRIPT="$INNATE_OS_ROOT/scripts/webapp_uri.zsh"
if command -v zsh >/dev/null 2>&1 && [ -f "$WEBAPP_URI_SCRIPT" ]; then
  WEBAPP_URI="$(zsh "$WEBAPP_URI_SCRIPT" --print 2>/dev/null || true)"
  if [ -n "$WEBAPP_URI" ]; then
    echo "Web app: $WEBAPP_URI" >&2
  fi
fi
exit 0

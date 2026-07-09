#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Installs the SLEEPNET deep-sleep experiment:
#   - adds the SLEEPNET power mode (ID=4) to /etc/nvpmodel.conf (idempotent,
#     original backed up once to /etc/nvpmodel.conf.innate-bak)
#   - installs innate-deep-sleep + the wake listener to /usr/local/sbin
#     (root-owned copies) and its systemd unit (not enabled — `enter` does that)

set -eu
[ "$(id -u)" = "0" ] || { echo "run as root: sudo $0" >&2; exit 1; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF=/etc/nvpmodel.conf

if grep -q "NAME=SLEEPNET" "$CONF"; then
    echo "SLEEPNET mode already present in $CONF"
else
    [ -f "$CONF.innate-bak" ] || cp "$CONF" "$CONF.innate-bak"
    # Insert the mode block before the (single, uncommented) PM_CONFIG section.
    awk -v blockfile="$SRC_DIR/nvpmodel-sleepnet.conf" '
        /^< PM_CONFIG/ && !done { while ((getline line < blockfile) > 0) print line; print ""; done=1 }
        { print }
    ' "$CONF" > "$CONF.tmp"
    grep -q "NAME=SLEEPNET" "$CONF.tmp"  # sanity before replacing
    mv "$CONF.tmp" "$CONF"
    echo "Added SLEEPNET (ID=4) to $CONF (backup: $CONF.innate-bak)"
fi

install -o root -g root -m 755 "$SRC_DIR/deep-sleep.sh" /usr/local/sbin/innate-deep-sleep
install -o root -g root -m 644 "$SRC_DIR/wake-listener.py" /usr/local/sbin/innate-deepsleep-wake-listener.py
install -o root -g root -m 644 "$SRC_DIR/innate-deepsleep-wake.service" /etc/systemd/system/innate-deepsleep-wake.service
systemctl daemon-reload

echo "Installed. Verify the mode parses:  sudo nvpmodel -p --verbose | grep SLEEPNET"
echo "Enter deep sleep (reboots):         sudo innate-deep-sleep enter"
echo "Wake over the network:              curl -X POST http://<robot>:4022/wake"

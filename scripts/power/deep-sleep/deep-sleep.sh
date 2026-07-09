#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Deep sleep for the robot: the whole ROS stack goes down, the PCB gates the
# HSSW rail (arm/head Dynamixels + wheel drivers off), the Jetson reboots into
# the custom SLEEPNET nvpmodel mode (2 cores / 422 MHz / EMC 665 MHz), and only
# the network + a tiny HTTP wake listener stay up.
#
#   enter — gate the rail, disable the robot stack, arm the wake listener,
#           reboot into SLEEPNET
#   wake  — un-gate the rail, re-enable the robot stack, disarm the listener,
#           reboot into MAXN_SUPER
#
# A reboot each way is unavoidable: nvpmodel refuses to change the online-core
# set on a live system (it prompts for a reboot; we answer YES). The chosen
# mode persists across the reboot via /var/lib/nvpmodel/status, the systemd
# enable/disable states select which stack comes up, and the MCU holds the rail
# gate across the reboot (firmware README §4.5). The rail is (un)gated with the
# ROS stack DOWN so nothing contends for the I2C bus; on wake it is restored
# BEFORE the reboot so mars_arm finds powered servos to initialize on boot.

set -eu
[ "$(id -u)" = "0" ] || { echo "run as root" >&2; exit 1; }

MODE_SLEEP=4  # SLEEPNET (installed by install.sh)
MODE_AWAKE=2  # MAXN_SUPER

ROBOT_SERVICES="ros-app.service jetson-perf.service speaker-keepalive.service"
LISTENER=innate-deepsleep-wake.service
PCB_POWER=/usr/local/sbin/innate-deepsleep-pcb-power

case "${1:-}" in
    enter)
        systemctl disable $ROBOT_SERVICES
        systemctl stop ros-app.service           # free the I2C bus before gating
        python3 "$PCB_POWER" sleep               # cut the HSSW rail (Dynamixels + drivers)
        systemctl enable "$LISTENER"
        echo "Entering deep sleep: rail gated, rebooting into SLEEPNET (POST /wake on :4022)"
        echo YES | /usr/sbin/nvpmodel -m "$MODE_SLEEP"
        ;;
    wake)
        python3 "$PCB_POWER" wake                 # restore the rail before the stack boots
        systemctl enable $ROBOT_SERVICES
        systemctl disable "$LISTENER"
        echo "Waking: rail restored, rebooting into MAXN_SUPER with the robot stack enabled"
        echo YES | /usr/sbin/nvpmodel -m "$MODE_AWAKE"
        ;;
    *)
        echo "usage: $0 enter|wake" >&2
        exit 1
        ;;
esac

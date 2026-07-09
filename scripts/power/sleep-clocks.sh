#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Live CPU power reduction for robot sleep mode. Called by the power_manager
# node via `sudo -n` (sudoers entry installed by scripts/update/post_update.sh).
#
# "sleep" mimics the CPU side of nvpmodel's 7W profile, which nvpmodel itself
# refuses to apply without a reboot: offline cores 4-5 and let the rest scale
# down to the lowest OPP (115 MHz idle, capped at 422 MHz under load — enough
# to keep zenoh/rosbridge responsive for wake triggers).
# "wake" undoes it; the wake sequence re-pins full performance right after via
# nvpmodel -m 2 + jetson-perf.

set -u

# Cores idle at the 115 MHz floor either way; the max only caps bursts. 268 MHz
# keeps zenoh/rosbridge/SSH usable — don't go to 115: wake itself starts at
# these clocks.
SLEEP_MAX_FREQ=268800
WAKE_MIN_FREQ=729600  # matches every nvpmodel profile's min

# EMC (memory) cap — VDD_SOC (~2.6 W) tracks EMC and is the largest fixed
# Jetson draw during sleep. 665.6 MHz is a standard Orin OPP; nothing needs
# bandwidth while asleep. The wake value matches MAXN_SUPER, and the wake
# sequence runs nvpmodel -m 2 right after, which rewrites the cap anyway.
EMC_CAP=/sys/kernel/nvpmodel_clk_cap/emc
SLEEP_EMC_FREQ=665600000
WAKE_EMC_FREQ=3199000000

case "${1:-}" in
    sleep)
        for cpu in 4 5; do
            echo 0 > "/sys/devices/system/cpu/cpu$cpu/online"
        done
        for policy in /sys/devices/system/cpu/cpufreq/policy*; do
            [ -f "$policy/scaling_max_freq" ] || continue  # fully-offline cluster
            cat "$policy/cpuinfo_min_freq" > "$policy/scaling_min_freq"
            echo "$SLEEP_MAX_FREQ" > "$policy/scaling_max_freq"
        done
        [ -w "$EMC_CAP" ] && echo "$SLEEP_EMC_FREQ" > "$EMC_CAP"
        ;;
    wake)
        [ -w "$EMC_CAP" ] && echo "$WAKE_EMC_FREQ" > "$EMC_CAP"
        for cpu in 4 5; do
            echo 1 > "/sys/devices/system/cpu/cpu$cpu/online"
        done
        for policy in /sys/devices/system/cpu/cpufreq/policy*; do
            [ -f "$policy/scaling_max_freq" ] || continue
            cat "$policy/cpuinfo_max_freq" > "$policy/scaling_max_freq"
            echo "$WAKE_MIN_FREQ" > "$policy/scaling_min_freq"
        done
        ;;
    *)
        echo "usage: $0 sleep|wake" >&2
        exit 1
        ;;
esac

exit 0

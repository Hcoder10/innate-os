#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Standalone PCB power control over I2C, for deep sleep.

Deep sleep tears the whole ROS stack down, so bringup's I2C driver isn't
available to gate the HSSW rail. This talks to the MCU (0x42, bus 1) directly
with the same CMD_POWER frame as mars_bringup/i2c.py (firmware README §4.5):

    sleep — mode 0x01, flags GATE_HSSW: cut the arm/head Dynamixel + wheel-
            driver rail. No wake-on-motion: deep sleep wakes over the network,
            so the IMU sleeps too for the lowest draw.
    wake  — mode 0x00: restore the rail.

The MCU holds the gate across the Jetson's reboot, so gate-then-reboot (enter)
and un-gate-then-reboot (wake) both land in the intended state. Best-effort:
an I2C error warns and still exits 0 so it can't wedge the sleep/wake reboot.

Run only when the ROS stack is DOWN — nothing else may drive the I2C bus.
"""

import sys

import smbus2

I2C_BUS = 1
MCU_ADDR = 0x42
REG = 0x00
CMD_POWER = 0x06
SLEEP_FLAG_GATE_HSSW = 0x01
CRC8_POLY = 0x8C  # CRC-8/MAXIM, matches i2c.py and the firmware


def _crc8(data: list[int]) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ CRC8_POLY if crc & 0x01 else crc >> 1
    return crc


def _send_power(mode: int, flags: int) -> None:
    payload = [CMD_POWER, mode, flags, 0x00, 0x00, 0x00, 0x00]  # cmd + 6 data bytes
    frame = payload + [_crc8(payload)]
    with smbus2.SMBus(I2C_BUS) as bus:
        bus.write_i2c_block_data(MCU_ADDR, REG, frame)


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "sleep":
        mode, flags = 0x01, SLEEP_FLAG_GATE_HSSW
    elif action == "wake":
        mode, flags = 0x00, 0x00
    else:
        print("usage: pcb-power.py sleep|wake", file=sys.stderr)
        return 1
    try:
        _send_power(mode, flags)
        print(f"PCB {action}: CMD_POWER mode={mode:#04x} flags={flags:#04x} sent")
    except Exception as e:  # best-effort — must not wedge the reboot
        print(f"WARN: PCB {action} over I2C failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

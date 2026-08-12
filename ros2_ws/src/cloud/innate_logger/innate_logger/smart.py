# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Disk health collection: `smartctl --json -x` per block device.

Pure functions — no ROS, no node state. Every reader returns whatever it could
read and swallows the rest, so one unreadable device never costs the sweep.

These arguments are spelled out verbatim in the sudoers rule, which is not
wildcarded — smartctl is far from read-only, and sudo matches arguments as one
string. Changing them here without changing post_update.sh loses sudo rights.
"""

from __future__ import annotations

import glob
import json
import subprocess
from typing import Any

SMARTCTL = "/usr/sbin/smartctl"
DEVICE_GLOBS = ("/dev/nvme?n?", "/dev/sd?")
_TIMEOUT = 30.0

# smartctl's exit status is a bitmask, not pass/fail: bits 3-7 mean "device is
# failing / has logged errors" and still come with the full report attached.
# Only bits 0-2 (command line error, open failed, SMART command failed) mean
# there is nothing to read.
_NO_DATA_BITS = 0b111


def collect() -> dict[str, Any]:
    """SMART data keyed by device path, for every block device present.

    Empty when nothing is readable — no smartctl, no devices, no sudo rights —
    so the caller can skip the report rather than send a wrapper full of errors.
    """
    report: dict[str, Any] = {}
    for device in devices():
        if data := _read_smart(device):
            report[device] = data
    return report


def devices() -> list[str]:
    """Every block device worth asking about, whether or not it answers."""
    return sorted(path for pattern in DEVICE_GLOBS for path in glob.glob(pattern))


def summarize(report: dict[str, Any]) -> str:
    """One log line: the few fields worth seeing on the robot's own console."""
    return " | ".join(f"{device.rsplit('/', 1)[-1]} {_summarize_device(data)}" for device, data in report.items())


def _read_smart(device: str) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["sudo", "-n", SMARTCTL, "--json=c", "-x", device],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode & _NO_DATA_BITS:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _summarize_device(data: dict[str, Any]) -> str:
    parts: list[str] = []
    if (temp := data.get("temperature", {}).get("current")) is not None:
        parts.append(f"{temp}C")
    if (hours := data.get("power_on_time", {}).get("hours")) is not None:
        parts.append(f"{hours}h")
    if (used := data.get("nvme_smart_health_information_log", {}).get("percentage_used")) is not None:
        parts.append(f"{used}% used")
    if (passed := data.get("smart_status", {}).get("passed")) is not None:
        parts.append("ok" if passed else "FAILING")
    return " ".join(parts) or "no data"

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Disk health: SMART per device, plus what every filesystem is doing for space.

Pure functions — no ROS, no node state. Every reader returns whatever it could
read and swallows the rest, so one unreadable device never costs the sweep.

A dying disk and a full one are different failures and the second is the more
likely, so the report carries both. SMART needs root via sudo; the filesystem
side needs neither root nor smartctl.
"""

from __future__ import annotations

import glob
import json
import subprocess
from typing import Any

import psutil

SMARTCTL = "/usr/sbin/smartctl"
DEVICE_GLOBS = ("/dev/nvme?n?", "/dev/sd?")
# Loopback snaps and kernel filesystems are noise; only real storage is reported.
SKIP_FSTYPES = frozenset({"squashfs", "tmpfs", "devtmpfs", "overlay", "ramfs", "efivarfs"})
_TIMEOUT = 30.0

# smartctl's exit status is a bitmask, not pass/fail: bits 3-7 mean "device is
# failing / has logged errors" and still come with the full report attached.
# Only bits 0-2 (command line error, open failed, SMART command failed) mean
# there is nothing to read.
_NO_DATA_BITS = 0b111


def collect() -> dict[str, Any]:
    """The hourly disk report: SMART per device, every filesystem, and TRIM state."""
    return {
        "smart": {device: data for device in devices() if (data := _read_smart(device))},
        "filesystems": filesystems(),
        "fstrim_timer": _fstrim_timer(),
    }


def devices() -> list[str]:
    """Every block device worth asking about, whether or not it answers."""
    return sorted(path for pattern in DEVICE_GLOBS for path in glob.glob(pattern))


def filesystems() -> list[dict[str, Any]]:
    """Size, fullness and continuous-TRIM state of every real mounted filesystem.

    `discard` here is the mount option only. Most systems TRIM in batches from
    fstrim.timer instead, so an absent option is not an absent TRIM.
    """
    mounts: list[dict[str, Any]] = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in SKIP_FSTYPES or part.device.startswith("/dev/loop"):
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        mounts.append(
            {
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_mb": usage.total // 1048576,
                "used_mb": usage.used // 1048576,
                "percent": usage.percent,
                "discard": "discard" in part.opts.split(","),
            }
        )
    return mounts


def summarize(report: dict[str, Any]) -> str:
    """One log line: the few fields worth seeing on the robot's own console."""
    parts = [f"{device.rsplit('/', 1)[-1]} {_summarize_device(data)}" for device, data in report["smart"].items()]
    parts += [f"{fs['mountpoint']} {fs['percent']:.0f}% of {fs['total_mb'] // 1024}G" for fs in report["filesystems"]]
    return " | ".join(parts)


def _fstrim_timer() -> str:
    """Whether periodic TRIM is scheduled — the other half of the discard question."""
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", "fstrim.timer"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


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

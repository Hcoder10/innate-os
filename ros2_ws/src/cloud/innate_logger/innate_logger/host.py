# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Host vitals the OS hands out for free: memory, load, uptime, temperatures.

Pure functions — no ROS, no node state. Cheap enough to call on every tick.
"""

from __future__ import annotations

import glob
import os
import time
from typing import Any

import psutil

THERMAL_ZONES = "/sys/class/thermal/thermal_zone*"


def resources() -> dict[str, Any]:
    """Memory, swap, load and uptime — the four that make an OOM or a reboot loop visible."""
    memory = psutil.virtual_memory()
    load_1, load_5, load_15 = os.getloadavg()
    return {
        "memory_percent": memory.percent,
        "memory_available_mb": memory.available // 1048576,
        "swap_percent": psutil.swap_memory().percent,
        "load_1m": round(load_1, 2),
        "load_5m": round(load_5, 2),
        "load_15m": round(load_15, 2),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
    }


def temperatures() -> dict[str, float]:
    """Every thermal zone the kernel exposes, keyed by its own name.

    Read by zone `type` rather than a fixed list — the Jetson's zone names are
    nothing like a desktop's, and a throttling SOC is worth seeing either way.
    """
    readings: dict[str, float] = {}
    for zone in sorted(glob.glob(THERMAL_ZONES)):
        name = _read_text(f"{zone}/type")
        millidegrees = _read_text(f"{zone}/temp")
        if name is None or millidegrees is None:
            continue
        try:
            readings[name] = int(millidegrees) / 1000.0
        except ValueError:
            continue
    return readings


def _read_text(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None

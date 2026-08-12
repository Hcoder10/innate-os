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
    """Memory headline, load and uptime — enough to see an OOM or a reboot loop.

    `percent` is derived from MemAvailable, so it already discounts reclaimable
    page cache; `free` would read as an emergency on a healthy machine.
    """
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


def memory_detail() -> dict[str, int]:
    """The full breakdown, for telling real pressure apart from page cache.

    Orin's GPU has no memory of its own — it shares these totals — so there is
    no separate figure to read here; attributing a slice of it to the GPU needs
    tegrastats or debugfs.

    `swap_in_bytes`/`swap_out_bytes` are cumulative since boot: the delta
    between two samples is the thrash rate, which is the number that matters.
    """
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "memory_total_mb": memory.total // 1048576,
        "memory_used_mb": memory.used // 1048576,
        "memory_free_mb": memory.free // 1048576,
        "memory_cached_mb": memory.cached // 1048576,
        "memory_buffers_mb": memory.buffers // 1048576,
        "memory_shared_mb": memory.shared // 1048576,
        "memory_slab_mb": memory.slab // 1048576,
        "swap_total_mb": swap.total // 1048576,
        "swap_used_mb": swap.used // 1048576,
        "swap_in_bytes": swap.sin,
        "swap_out_bytes": swap.sout,
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
            value = int(millidegrees) / 1000.0
        except ValueError:
            continue
        if name in readings:  # boards repeat a type (acpitz, per-cluster CPU zones)
            name = f"{name}:{zone.rsplit('thermal_zone', 1)[-1]}"
        readings[name] = value
    return readings


def _read_text(path: str) -> str | None:
    """Read a small sysfs attribute, or None if the kernel will not answer.

    os.read rather than open().read(): a thermal zone whose sensor is asleep
    fails the read, and buffered text IO turns that into `TypeError: can't
    concat NoneType to bytes` from inside codecs -- which no OSError handler
    catches, and which took the whole node down on a Jetson.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        return os.read(fd, 4096).decode("utf-8", "replace").strip() or None
    except OSError:
        return None
    finally:
        os.close(fd)

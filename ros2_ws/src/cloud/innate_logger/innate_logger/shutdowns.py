# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The boot sentinel's verdict on the last shutdown, for the vitals payload.

Pure functions -- no ROS, no node state. All the work happens in
``scripts/boot_sentinel.py``, which runs as root from a systemd unit at boot
and at shutdown; this side only reads what it left behind. Splitting it that
way is what keeps the node out of the journal-permission question entirely, and
means the analysis has already happened by the time anything here runs.

The drive's own ``unsafe_shutdowns`` (see disks.py) counts dirty cuts and the
sentinel attributes them; neither replaces the other, and the two are worth
comparing precisely because they are independent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Written by scripts/boot_sentinel.py. Duplicated rather than shared: that
# script is stdlib-only and runs before the ROS workspace exists to import from.
REPORT_PATH = Path("/var/lib/innate/boot/last_boot.json")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def report() -> dict[str, Any] | None:
    """This boot's record of the previous shutdown, or None if there isn't one.

    None covers every way the sentinel can be absent -- never installed, unit
    disabled, `innate build` without the `innate update` that installs it -- and
    they are all the same thing to the caller: no verdict to send.
    """
    try:
        loaded = json.loads(REPORT_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None

    # A report from an earlier boot means the sentinel did not run for this one,
    # so it describes a shutdown that has already been reported. Sending it
    # again would count one reset twice, every boot, for as long as the unit
    # stays broken -- which is exactly the direction that would mislead.
    current = _boot_id()
    if current is not None and loaded.get("boot_id") != current:
        return None

    # pstore_seen is bookkeeping for the sentinel's own deduplication across
    # boots and means nothing to the cloud.
    return {key: value for key, value in loaded.items() if key != "pstore_seen"}


def summarize(record: dict[str, Any]) -> str:
    """One log line: what the robot's own console should say about its last reset."""
    cause = record.get("cause", "unknown")
    counted = f"{record.get('ungraceful_count', '?')}/{record.get('boot_count', '?')} ungraceful"
    if cause == "clean":
        return f"last shutdown clean ({counted})"
    detail = f"flag {record.get('flag_verdict')}, journal {record.get('journal_verdict')}"
    return f"last shutdown {cause.upper()} — {detail} ({counted})"


def _boot_id() -> str | None:
    try:
        return BOOT_ID_PATH.read_text().strip() or None
    except OSError:
        return None

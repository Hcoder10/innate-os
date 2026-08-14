# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Who is running this simulator, and which build of it.

Everything here is resolved on the *host*. The container sees a handful of
mounted subdirectories, not the repo — no `.git`, and its own throwaway
`/etc/machine-id` and network interfaces. Anything derived from those inside
the container would be wrong in a way that quietly inflates install counts, so
the launcher computes them once and passes them down through the generated env
file.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
import uuid
from pathlib import Path

from config import (
    INSTALL_ID_PATH,
    REPO_ROOT,
    ensure_state_dir,
)

# Domain separation, not a secret — this is an open-source repo, so the salt is
# public by construction. It is safe here because the inputs are 128-bit random
# machine UUIDs: unlike a MAC address (a ~48-bit space with a known vendor
# prefix, brute-forceable in seconds) they cannot be recovered from the digest.
# That is the reason to hash a machine id rather than a MAC.
_DEVICE_SALT = "innate-sim-device-v1"

_IOREG_UUID_RE = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')


def install_id() -> str:
    """A UUID identifying this checkout, created on first use.

    Persisted beside the launcher's other state, so it survives restarts but
    not a fresh clone — which is the intent. Counting *machines* is
    :func:`device_hash`'s job.
    """
    ensure_state_dir()
    if INSTALL_ID_PATH.exists():
        existing = INSTALL_ID_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    fresh = str(uuid.uuid4())
    INSTALL_ID_PATH.write_text(f"{fresh}\n", encoding="utf-8")
    return fresh


def _machine_id() -> str:
    """The host OS's own stable machine identifier, or "" if unavailable.

    Deliberately not a MAC address: those change with the active interface, are
    randomised per-container by Docker, and are personal data.
    """
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
        found = _IOREG_UUID_RE.search(out)
        return found.group(1) if found else ""

    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value

    return ""


def device_hash() -> str:
    """A stable, non-reversible id for this machine, or "" if undeterminable.

    Two checkouts on one laptop share this, so installs can be collapsed into
    devices. Under WSL the distro's machine-id differs from Windows' own, so a
    user running both is counted as two devices.
    """
    machine = _machine_id()
    if not machine:
        return ""
    return hashlib.sha256(f"{_DEVICE_SALT}:{machine}".encode("utf-8")).hexdigest()[:16]


def host_platform() -> str:
    """darwin | wsl | linux | windows | <sys.platform>."""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        # WSL is worth separating out: its filesystem and network latency make
        # it the platform most likely to show a poor real-time factor.
        return "wsl" if "microsoft" in platform.uname().release.lower() else "linux"
    return sys.platform


def platform_release() -> str:
    return f"{platform.system()} {platform.release()}"[:128]


def _git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def repo_commit() -> str:
    """Short HEAD of the checkout, or "" outside a git tree."""
    return _git("rev-parse", "--short", "HEAD")


def launcher_version() -> str:
    """A human-readable version for the checkout (tag if there is one)."""
    return _git("describe", "--tags", "--always", "--dirty")


def telemetry_enabled(env: dict[str, str]) -> bool:
    """Opt-out switch, honoured from the shell as well as `.env`.

    Anything but a clear "off" counts as on, so a typo does not silently
    disable reporting.
    """
    raw = (os.environ.get("INNATE_TELEMETRY") or env.get("INNATE_TELEMETRY") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}

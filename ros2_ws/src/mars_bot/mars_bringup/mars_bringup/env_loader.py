#!/usr/bin/env python3
"""
Environment loader for Innate-OS.

Loads the ``.env`` file (machine-local secrets + optional env overrides) into the
process environment. Non-secret robot tunables now live in the ``innate_config``
``overrides.yaml`` and are delivered to nodes as ROS parameters — not here.
"""

import os
import sys
from pathlib import Path


def _load_key_value_env(path: Path) -> None:
    if not path.exists():
        return
    try:
        f = open(path)
    except OSError as e:
        # Transient mount errors (e.g. Docker Desktop single-file bind EPERM)
        # would otherwise crash launch-file loading. Treat as "no env file".
        print(f"[env_loader] Could not open {path}: {e}", file=sys.stderr)
        return
    with f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                os.environ[key] = value


# Keys whose presence in a local config/os.toml used to configure the robot.
# os.toml no longer feeds the robot (brain/telemetry/voice are tuned in
# innate_config/overrides.yaml, extra scan dirs in .env); warn so an operator
# with a populated os.toml isn't surprised by a silently-ignored value.
_DEPRECATED_OS_TOML_KEYS = ("websocket_uri", "url", "cartesia_voice_id", "agent_dirs", "skill_dirs")


def _warn_deprecated_os_toml(innate_root: Path) -> None:
    path = innate_root / "config" / "os.toml"
    if not path.is_file():
        return
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for raw in lines:
        s = raw.strip()
        if s and not s.startswith("#") and "=" in s and any(k in s for k in _DEPRECATED_OS_TOML_KEYS):
            print(
                "[env_loader] config/os.toml no longer configures the robot — move brain/"
                "telemetry/voice into innate_config/overrides.yaml and scan dirs into .env.",
                file=sys.stderr,
            )
            return


def load_env_file(env_path: Path | None = None) -> None:
    """Load environment variables from the ``.env`` file.

    Args:
        env_path: Optional path to ``.env``. If not provided, uses ``INNATE_OS_ROOT``
                  or defaults to ``~/innate-os/.env``.
    """
    if env_path is None:
        innate_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
        env_path = Path(innate_root) / ".env"

    _warn_deprecated_os_toml(env_path.parent)
    _load_key_value_env(env_path)


def get_env(key: str, default: str = "") -> str:
    """Get an environment variable, returning ``default`` if unset."""
    return os.environ.get(key, default)


# Load env file on module import
if __name__ == "__main__":
    load_env_file()

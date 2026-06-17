"""Resolve the path to the central parameter override file.

The override file is read from the live source tree under ``INNATE_OS_ROOT`` so
edits take effect on the next node start without a ``colcon build``. Production
updates run a plain ``colcon build`` (no ``--symlink-install``), which copies
configs into ``install/``; resolving from source avoids that stale copy shadowing
a live edit. The installed share copy is only used as a fallback when the source
tree is unavailable (e.g. a packaged/container deploy without the repo mounted).
"""

import os
from pathlib import Path

_OVERRIDES_REL = "ros2_ws/src/innate_config/config/overrides.yaml"


def _innate_os_root() -> Path:
    return Path(os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")))


def overrides_path() -> str:
    """Absolute path to the central ``overrides.yaml``.

    Prefers the live source file under ``INNATE_OS_ROOT``; falls back to the
    installed ``share/innate_config/config/overrides.yaml``.
    """
    source = _innate_os_root() / _OVERRIDES_REL
    if source.is_file():
        return str(source)

    from ament_index_python.packages import get_package_share_directory

    return os.path.join(get_package_share_directory("innate_config"), "config", "overrides.yaml")

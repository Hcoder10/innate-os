"""Filesystem paths for the central parameter override system.

Single source of truth for where the override + registry files live; both the
ROS-free registry module and the launch helper import from here.

Files resolve from the live source tree under ``INNATE_OS_ROOT`` first, so a
hand-edit to ``overrides.yaml`` takes effect on the next node start without a
``colcon build``. The installed ``share/`` copy is only a fallback for packaged
deploys where the repo is not mounted.
"""

import os
from pathlib import Path

_OVERRIDES_REL = "ros2_ws/src/innate_config/config/overrides.yaml"
_REGISTRY_REL = "ros2_ws/src/innate_config/config/registry.yaml"

# Internal, machine-generated ROS params file derived from overrides.yaml.
# Not user-edited (dotfile) and git-ignored; lives next to overrides.yaml.
_GENERATED_NAME = ".overrides.generated.yaml"


def innate_os_root() -> Path:
    return Path(os.environ.get("INNATE_OS_ROOT", Path.home() / "innate-os"))


def _resolve(rel: str, share_name: str) -> Path:
    source = innate_os_root() / rel
    if source.is_file():
        return source

    from ament_index_python.packages import get_package_share_directory

    return Path(get_package_share_directory("innate_config")) / "config" / share_name


def overrides_path() -> Path:
    """User-edited friendly overrides file (``nav.max_speed: 0.3``)."""
    return _resolve(_OVERRIDES_REL, "overrides.yaml")


def registry_path() -> Path:
    """Read-only knob registry (friendly name -> ROS node/param + metadata)."""
    return _resolve(_REGISTRY_REL, "registry.yaml")


def generated_path() -> Path:
    """Preferred location for the machine-generated node-keyed ROS params file.

    Sits next to whichever ``overrides.yaml`` resolved. The launch helper falls
    back to a temp dir if that directory is read-only.
    """
    return overrides_path().parent / _GENERATED_NAME

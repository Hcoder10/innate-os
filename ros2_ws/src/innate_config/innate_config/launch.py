"""Launch helper: translate the friendly overrides file into ROS parameters.

Users tune the robot by hand-editing ``overrides.yaml`` with friendly names
(``nav.max_speed: 0.3``). At launch we translate those into a node-keyed ROS
parameter file and append it last, so an override wins over the package default
while any knob left untouched falls through. Pass the result straight to
``Node(parameters=...)`` / ``ComposableNode(parameters=...)``::

    parameters=apply_overrides([planner_params_file, costmap_params_file])
"""

import os
import sys
import tempfile
from pathlib import Path

import yaml

from innate_config.paths import generated_path

_GENERATED_HEADER = (
    "# AUTO-GENERATED from overrides.yaml — do not edit.\n"
    "# Edit ros2_ws/src/innate_config/config/overrides.yaml instead.\n"
)

# Cached for the lifetime of a launch process: the friendly file does not change
# mid-launch, so we render the node-keyed file (and print warnings) only once.
_generated_cache = None


def _write_generated():
    """Render the node-keyed override file from overrides.yaml; return its path.

    Fails open: on any error returns ``None`` so launch proceeds with package
    defaults rather than crashing the whole robot over a config problem.
    """
    global _generated_cache
    if _generated_cache is not None:
        return _generated_cache

    try:
        from innate_config.registry import build_node_params

        node_params, warnings = build_node_params()
        for w in warnings:
            print(f"[innate_config] {w}", file=sys.stderr)

        text = _GENERATED_HEADER + yaml.safe_dump(node_params, default_flow_style=False, sort_keys=True)

        target = generated_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            target = Path(tempfile.gettempdir()) / target.name

        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, target)  # atomic: readers see old or new, never a torn file

        _generated_cache = str(target)
    except Exception as exc:  # noqa: BLE001 - never break launch over config
        print(f"[innate_config] could not apply overrides: {exc}", file=sys.stderr)
        return None
    return _generated_cache


def apply_overrides(params=None):
    """Append the generated override file so it layers on top of package configs."""
    merged = list(params) if params else []
    generated = _write_generated()
    if generated:
        merged.append(generated)
    return merged

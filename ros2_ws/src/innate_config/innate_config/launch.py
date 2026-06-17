"""Launch helper to layer the central override file on top of package configs."""

from innate_config.paths import overrides_path


def apply_overrides(params=None):
    """Append the central override file so it layers on top of package defaults.

    ROS 2 merges parameter sources left to right, so the override is placed last:
    its values win, while any key it does not set falls through to the package
    config. Pass the result straight to ``Node(parameters=...)`` /
    ``ComposableNode(parameters=...)``.

        parameters=apply_overrides([planner_params_file, costmap_params_file])
    """
    merged = list(params) if params else []
    merged.append(overrides_path())
    return merged

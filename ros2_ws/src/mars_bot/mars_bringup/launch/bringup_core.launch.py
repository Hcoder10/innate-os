# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node
from mars_bringup.config_loader import settings_params


def generate_launch_description():
    # Get the package share directory
    pkg_dir = get_package_share_directory("mars_bringup")

    # Path to the config file
    config_file = os.path.join(pkg_dir, "config", "robot_config.yaml")

    bringup_node = Node(
        package="mars_bringup",
        executable="bringup.py",
        name="bringup",
        parameters=[config_file, *settings_params()],
        output="screen",
    )

    # Sleep/wake orchestration (light sleep) — see mars_bringup/power_manager.py
    power_manager_node = Node(
        package="mars_bringup",
        executable="power_manager.py",
        name="power_manager",
        parameters=[*settings_params()],
        output="screen",
    )

    # base_link -> base_footprint static TF is now published by
    # robot_state_publisher via the URDF (base_footprint_joint).

    return LaunchDescription(
        [
            bringup_node,
            power_manager_node,
        ]
    )

# SPDX-License-Identifier: Apache-2.0
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node
from mars_bringup.config_loader import settings_params


def generate_launch_description():
    # Get the package share directory
    pkg_dir = get_package_share_directory("mars_control")

    # Path to the config file
    config_file = os.path.join(pkg_dir, "config", "motion_control.yaml")

    keyboard_node = Node(
        package="mars_control",
        executable="keyboard.py",
        name="keyboard_controller",
        parameters=[config_file, *settings_params()],
        output="screen",
    )

    return LaunchDescription([keyboard_node])

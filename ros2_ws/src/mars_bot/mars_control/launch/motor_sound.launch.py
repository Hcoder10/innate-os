# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node
from mars_bringup.config_loader import settings_params


def generate_launch_description():
    pkg_dir = get_package_share_directory("mars_control")
    config_file = os.path.join(pkg_dir, "config", "motor_sound.yaml")

    motor_sound_node = Node(
        package="mars_control",
        executable="motor_sound.py",
        name="motor_sound",
        parameters=[config_file, *settings_params()],
        output="screen",
    )

    return LaunchDescription([motor_sound_node])

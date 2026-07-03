# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Launch file for the innate_console node."""

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            Node(
                package="innate_console",
                executable="console_node",
                name="innate_console",
                output="screen",
                parameters=[],
            ),
        ]
    )

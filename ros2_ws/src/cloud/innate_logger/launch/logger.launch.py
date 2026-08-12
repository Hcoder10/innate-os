# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Launch file for the innate_logger node."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    log_level = LaunchConfiguration("log_level")
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "log_level",
                default_value="warn",
                description="Node log level. 'info' also dumps every telemetry body as it is sent.",
            ),
            Node(
                package="innate_logger",
                executable="logger_node",
                name="logger_node",
                output="screen",
                # telemetry_url comes from the environment (TELEMETRY_URL in .env).
                parameters=[],
                arguments=["--ros-args", "--log-level", ["logger_node:=", log_level]],
            ),
        ]
    )

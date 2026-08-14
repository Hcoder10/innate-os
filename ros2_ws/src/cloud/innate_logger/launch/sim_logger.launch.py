# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Launch file for the innate_logger sim usage node."""

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
                default_value="info",
                description="Node log level. 'debug' also reports every failed telemetry attempt.",
            ),
            Node(
                package="innate_logger",
                executable="sim_logger_node",
                name="sim_logger_node",
                output="screen",
                # Everything comes from the environment: the launcher writes
                # INNATE_SIM_* into the .env the container mounts.
                parameters=[],
                arguments=["--ros-args", "--log-level", ["sim_logger_node:=", log_level]],
            ),
        ]
    )

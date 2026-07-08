#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Node to launch the RPLidar driver
    lidar_node = Node(
        package="rplidar_ros",
        executable="rplidar_node",
        name="rplidar_node",
        parameters=[
            {
                "channel_type": "serial",
                "serial_port": "/dev/rplidar",
                "serial_baudrate": 115200,
                "frame_id": "base_laser",
                "inverted": False,
                "angle_compensate": True,
                "scan_mode": "Express",
            }
        ],
        output="screen",
        remappings=[("scan", "scan_fast")],
    )

    # base_link -> base_laser static TF is now published by
    # robot_state_publisher via the URDF (base_laser_joint).

    # Node to throttle scan_fast into scan.
    # The lidar actually delivers ~8 Hz (10 Hz nominal); topic_tools throttle
    # passes a message only when 1/rate has elapsed, so asking for 6.0 from an
    # 8 Hz stream beat down to every other scan = 4 Hz in practice. 4.0 keeps
    # the same effective rate every consumer (AMCL, costmaps) was already
    # getting, but intentionally and without the beat-frequency surprise.
    throttle_node = Node(
        package="topic_tools",
        executable="throttle",
        name="scan_throttle",
        arguments=["messages", "/scan_fast", "4.0", "/scan"],
        output="screen",
    )

    return LaunchDescription(
        [
            lidar_node,
            throttle_node,
        ]
    )

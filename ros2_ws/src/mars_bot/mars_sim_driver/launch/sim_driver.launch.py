"""Virtual MARS: the MuJoCo sim driver + robot_state_publisher fed the same
mars.urdf the real bringup uses (static frames: base_footprint, base_laser,
camera_optical_frame, arm links). AMCL owns map->odom; the driver broadcasts
odom->base_link itself, like the real base driver."""

import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import SetEnvironmentVariable
from launch_ros.actions import Node


def generate_launch_description():
    urdf = Path(get_package_share_directory("mars_sim")) / "urdf" / "mars.urdf"

    return LaunchDescription(
        [
            # Software GL: the sim container has no GPU.
            SetEnvironmentVariable("MUJOCO_GL", os.environ.get("MUJOCO_GL", "osmesa")),
            Node(
                package="robot_state_publisher",
                executable="robot_state_publisher",
                name="robot_state_publisher",
                output="screen",
                parameters=[{"robot_description": urdf.read_text()}],
            ),
            Node(
                package="mars_sim_driver",
                executable="sim_driver",
                name="virtual_mars",
                output="screen",
            ),
            # Stand-in for the CUDA-only grid_localizer: satisfies the mode
            # manager's lifecycle + `localize` Trigger, seeding AMCL with the
            # ground-truth pose.
            Node(
                package="mars_sim_driver",
                executable="grid_localizer_sim",
                output="screen",
            ),
        ]
    )

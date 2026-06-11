#!/usr/bin/env python3
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Locate the package share directory and the YAML configuration file.
    pkg_share = get_package_share_directory("manipulation")
    config_file = os.path.join(pkg_share, "config", "recorder.yaml")

    # Resolve data_directory from $INNATE_OS_ROOT so a non-default repo
    # location is honored. ROS YAML can't expand env vars, so we override
    # the parameter here.
    innate_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
    data_directory = os.path.join(innate_root, "workspace", "custom_skills")

    # Define the C++ recorder node with its parameters.
    recorder_node = Node(
        package="manipulation",
        executable="recorder_node_cpp",
        name="recorder_node",
        output="screen",
        parameters=[config_file, {"data_directory": data_directory}],
    )

    return LaunchDescription([recorder_node])

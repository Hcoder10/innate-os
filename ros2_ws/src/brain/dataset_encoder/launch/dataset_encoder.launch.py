import os

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    # Mirror the recorder's skills root so the two stay in sync; ROS YAML can't
    # expand env vars, so resolve it here.
    innate_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
    skills_root = os.path.join(innate_root, "workspace", "custom_skills")

    return LaunchDescription(
        [
            Node(
                package="dataset_encoder",
                executable="dataset_encoder_node",
                name="dataset_encoder",
                output="screen",
                parameters=[{"skills_root": skills_root}],
            ),
        ]
    )

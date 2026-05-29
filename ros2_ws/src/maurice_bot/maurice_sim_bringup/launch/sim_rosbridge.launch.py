from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Keep the simulator bridge on the same RMW as the rest of the sim nodes.
    rosbridge_node = Node(
        package='rosbridge_server',
        executable='rosbridge_websocket',
        name='rosbridge_websocket',
        output='screen',
        parameters=[{
            'port': 9090,
        }]
    )

    return LaunchDescription([rosbridge_node])

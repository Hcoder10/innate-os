from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription(
        [
            Node(
                package="maurice_cam",
                executable="webrtc_streamer_node",
                name="webrtc_streamer",
                output="screen",
                parameters=[
                    {
                        "live_main_camera_topic": "/mars/main_camera/left/image_raw",
                        "live_arm_camera_topic": "/mars/arm/image_raw",
                        "replay_main_camera_topic": "/brain/recorder/replay/main_camera/left/image_raw",
                        "replay_arm_camera_topic": "/brain/recorder/replay/arm_camera/image_raw",
                        # Stream the robot microphone to the teleoperator. Set
                        # enable_audio False on units without a capture device, or
                        # override audio_capture_device (e.g. "hw:1,0") to pick a card.
                        "enable_audio": True,
                        "audio_source_element": "alsasrc",
                        "audio_capture_device": "",
                    }
                ],
            )
        ]
    )

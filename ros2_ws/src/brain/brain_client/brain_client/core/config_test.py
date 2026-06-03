"""Unit tests for BrainConfig derived properties (no ROS required).

``BrainConfig.load(node)`` needs a ROS node, but the dataclass and its derived
properties are pure and tested here by constructing an instance directly.
"""

import math

from brain_client.core.config import BrainConfig

_BASE = dict(
    websocket_uri="ws://x",
    token="t",
    image_topic="/img",
    cmd_vel_topic="/cmd_vel",
    depth_image_topic="/depth",
    amcl_pose_topic="/amcl",
    map_topic="/map",
    arm_camera_image_topic="/arm",
    odom_topic="/odom",
    current_nav_mode_topic="/mode",
    send_depth=True,
    send_arm_camera_image=True,
    use_odom_as_amcl_pose=False,
    send_video_feed=False,
    log_everything=False,
    simulator_mode=False,
    vertical_fov=60.0,
    horizontal_resolution=640,
    vertical_resolution=480,
    x_cam=0.1,
    height_cam=0.5,
    pose_image_interval=0.5,
    video_buffer_duration_seconds=2.0,
    video_fps=10.0,
    image_buffer_max_size=60,
    cartesia_voice_id="v",
    openai_realtime_model="m",
    openai_realtime_url="wss://r",
    openai_transcribe_model="tm",
)


def test_horizontal_fov_wider_than_vertical_for_landscape():
    cfg = BrainConfig(**_BASE)
    # 4:3 landscape -> horizontal FOV exceeds the 60deg vertical FOV.
    assert cfg.horizontal_fov > 60.0
    assert math.isclose(cfg.horizontal_fov, 75.178, abs_tol=0.01)


def test_camera_info_contains_intrinsics():
    cfg = BrainConfig(**_BASE)
    info = cfg.camera_info
    assert info["x_cam"] == 0.1
    assert info["height_cam"] == 0.5
    assert info["vertical_fov"] == 60.0
    assert "horizontal_fov" in info


def test_proxy_config_shape():
    cfg = BrainConfig(**_BASE)
    assert cfg.proxy_config == {
        "cartesia_voice_id": "v",
        "openai_realtime_model": "m",
        "openai_realtime_url": "wss://r",
        "openai_transcribe_model": "tm",
    }

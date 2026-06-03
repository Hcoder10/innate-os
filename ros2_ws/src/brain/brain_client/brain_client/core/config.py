"""Typed configuration for the brain client.

PURE module: imports no ``rclpy``. ``BrainConfig.load(node)`` is handed a node so
it can declare/read ROS parameters, but the dataclass itself is plain data — which
keeps every consumer (orchestrator, perception, comms) testable without a ROS
runtime and replaces the ~30 repetitions of
``self.get_parameter(x).get_parameter_value().y_value`` that used to live in
``BrainClientNode.__init__``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BrainConfig:
    # --- Backend / auth ---
    websocket_uri: str
    token: str

    # --- Topics ---
    image_topic: str
    cmd_vel_topic: str
    depth_image_topic: str
    amcl_pose_topic: str
    map_topic: str
    arm_camera_image_topic: str
    odom_topic: str
    current_nav_mode_topic: str

    # --- Feature flags ---
    send_depth: bool
    send_arm_camera_image: bool
    use_odom_as_amcl_pose: bool
    send_video_feed: bool
    log_everything: bool
    simulator_mode: bool

    # --- Camera geometry ---
    vertical_fov: float
    horizontal_resolution: int
    vertical_resolution: int
    x_cam: float
    height_cam: float

    # --- Timing / buffering ---
    pose_image_interval: float
    video_buffer_duration_seconds: float
    video_fps: float
    image_buffer_max_size: int

    # --- Proxy service config (credentials come from env, not params) ---
    cartesia_voice_id: str
    openai_realtime_model: str
    openai_realtime_url: str
    openai_transcribe_model: str

    @property
    def horizontal_fov(self) -> float:
        """Horizontal FOV derived from the vertical FOV and the aspect ratio."""
        return math.degrees(
            2
            * math.atan(
                math.tan(math.radians(self.vertical_fov) / 2) * self.horizontal_resolution / self.vertical_resolution
            )
        )

    @property
    def camera_info(self) -> dict:
        """Camera intrinsics block shared by image and pose-image payloads."""
        return {
            "horizontal_fov": self.horizontal_fov,
            "vertical_fov": self.vertical_fov,
            "x_cam": self.x_cam,
            "height_cam": self.height_cam,
        }

    @property
    def proxy_config(self) -> dict:
        return {
            "cartesia_voice_id": self.cartesia_voice_id,
            "openai_realtime_model": self.openai_realtime_model,
            "openai_realtime_url": self.openai_realtime_url,
            "openai_transcribe_model": self.openai_transcribe_model,
        }

    @classmethod
    def load(cls, node) -> BrainConfig:
        """Declare every parameter on ``node`` and read it into a frozen config.

        Defaults match the historical ``declare_parameter`` calls in
        ``BrainClientNode.__init__``. ``x_cam``/``height_cam`` are declared
        without a default (read back as 0.0 when unset), preserving prior
        behaviour where they are supplied via the launch file.
        """
        # name -> default (None means "declare without default")
        string_params = {
            "websocket_uri": "ws://localhost:8765",
            "token": "MY_HARDCODED_TOKEN",
            "image_topic": "/mars/main_camera/left/image_raw/compressed",
            "cmd_vel_topic": "/cmd_vel",
            "depth_image_topic": "/camera/depth/image_raw",
            "amcl_pose_topic": "/amcl_pose",
            "map_topic": "/map",
            "arm_camera_image_topic": "/mars/arm/image_raw/compressed",
            "odom_topic": "/odom",
            "current_nav_mode_topic": "/nav/current_mode",
            "cartesia_voice_id": "9fdaae0b-f885-4813-b589-3c07cf9d5fea",
            "openai_realtime_model": "gpt-4o-realtime-preview",
            "openai_realtime_url": "wss://api.openai.com/v1/realtime",
            "openai_transcribe_model": "gpt-4o-mini-transcribe",
        }
        bool_params = {
            "send_depth": True,
            "send_arm_camera_image": True,
            "use_odom_as_amcl_pose": False,
            "send_video_feed": False,
            "log_everything": False,
            "simulator_mode": False,
        }
        int_params = {
            "horizontal_resolution": 640,
            "vertical_resolution": 480,
            "image_buffer_max_size": 60,
        }
        double_params = {
            "vertical_fov": 60.0,
            "pose_image_interval": 0.5,
            "video_buffer_duration_seconds": 2.0,
            "video_fps": 10.0,
        }

        for name, default in {**string_params, **bool_params, **int_params, **double_params}.items():
            node.declare_parameter(name, default)
        # x_cam / height_cam: declared without a default (launch-provided).
        node.declare_parameter("x_cam")
        node.declare_parameter("height_cam")

        def s(name: str) -> str:
            return node.get_parameter(name).get_parameter_value().string_value

        def b(name: str) -> bool:
            return node.get_parameter(name).get_parameter_value().bool_value

        def i(name: str) -> int:
            return node.get_parameter(name).get_parameter_value().integer_value

        def d(name: str) -> float:
            return node.get_parameter(name).get_parameter_value().double_value

        return cls(
            websocket_uri=s("websocket_uri"),
            token=s("token"),
            image_topic=s("image_topic"),
            cmd_vel_topic=s("cmd_vel_topic"),
            depth_image_topic=s("depth_image_topic"),
            amcl_pose_topic=s("amcl_pose_topic"),
            map_topic=s("map_topic"),
            arm_camera_image_topic=s("arm_camera_image_topic"),
            odom_topic=s("odom_topic"),
            current_nav_mode_topic=s("current_nav_mode_topic"),
            send_depth=b("send_depth"),
            send_arm_camera_image=b("send_arm_camera_image"),
            use_odom_as_amcl_pose=b("use_odom_as_amcl_pose"),
            send_video_feed=b("send_video_feed"),
            log_everything=b("log_everything"),
            simulator_mode=b("simulator_mode"),
            vertical_fov=d("vertical_fov"),
            horizontal_resolution=i("horizontal_resolution"),
            vertical_resolution=i("vertical_resolution"),
            x_cam=d("x_cam"),
            height_cam=d("height_cam"),
            pose_image_interval=d("pose_image_interval"),
            video_buffer_duration_seconds=d("video_buffer_duration_seconds"),
            video_fps=d("video_fps"),
            image_buffer_max_size=i("image_buffer_max_size"),
            cartesia_voice_id=s("cartesia_voice_id"),
            openai_realtime_model=s("openai_realtime_model"),
            openai_realtime_url=s("openai_realtime_url"),
            openai_transcribe_model=s("openai_transcribe_model"),
        )

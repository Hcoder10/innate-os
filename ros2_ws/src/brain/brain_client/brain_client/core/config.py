# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Typed configuration for the brain client.

PURE module: imports no ``rclpy``. ``BrainConfig.load(node)`` is handed a node so
it can declare/read ROS parameters, but the dataclass itself is plain data —
which keeps every consumer testable without a ROS runtime.

Credentials deliberately stay out of the ROS parameter surface: the brain
reaches Gemini through the Innate proxy (INNATE_SERVICE_KEY) or directly via
the ``GEMINI_API_KEY`` environment variable (loaded from ``.env`` by launch).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrainConfig:
    # --- Topics ---
    image_topic: str
    cmd_vel_topic: str
    arm_camera_image_topic: str
    odom_topic: str
    current_nav_mode_topic: str
    scan_topic: str

    # --- Feature flags ---
    send_arm_camera_image: bool
    log_everything: bool
    simulator_mode: bool

    # --- Local brain (Gemini) ---
    gemini_model: str
    gemini_thinking_level: str  # "low" | "high"; "" = model default
    idle_turn_interval: float  # seconds between looks when no skill is running
    supervision_turn_interval: float  # seconds between looks while a skill runs
    history_max_entries: int  # conversation entries kept for the model
    history_max_image_turns: int  # newest turns that keep their camera frames

    # --- Timing ---
    scan_stale_after_sec: float

    # --- Proxy service config (credentials come from env, not params) ---
    cartesia_voice_id: str
    openai_realtime_model: str
    openai_realtime_url: str
    openai_transcribe_model: str

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
        """Declare every parameter on ``node`` and read it into a frozen config."""
        string_params = {
            "image_topic": "/mars/main_camera/left/image_raw/compressed",
            "cmd_vel_topic": "/cmd_vel",
            "arm_camera_image_topic": "/mars/arm/image_raw/compressed",
            "odom_topic": "/odom",
            "current_nav_mode_topic": "/nav/current_mode",
            "scan_topic": "/scan",
            "gemini_model": "gemini-3-flash-preview",
            # "" lets the model think at its default level. "low" is faster but
            # measurably hurts instruction-following (skill re-runs, chatter).
            "gemini_thinking_level": "",
            "cartesia_voice_id": "9fdaae0b-f885-4813-b589-3c07cf9d5fea",
            "openai_realtime_model": "gpt-4o-realtime-preview",
            "openai_realtime_url": "wss://api.openai.com/v1/realtime",
            "openai_transcribe_model": "gpt-4o-mini-transcribe",
        }
        bool_params = {
            "send_arm_camera_image": True,
            "log_everything": False,
            "simulator_mode": False,
        }
        int_params = {
            "history_max_entries": 60,
            "history_max_image_turns": 3,
        }
        double_params = {
            "idle_turn_interval": 3.0,
            "supervision_turn_interval": 5.0,
            "scan_stale_after_sec": 10.0,
        }

        for name, default in {**string_params, **bool_params, **int_params, **double_params}.items():
            node.declare_parameter(name, default)

        def s(name: str) -> str:
            return node.get_parameter(name).get_parameter_value().string_value

        def b(name: str) -> bool:
            return node.get_parameter(name).get_parameter_value().bool_value

        def i(name: str) -> int:
            return node.get_parameter(name).get_parameter_value().integer_value

        def d(name: str) -> float:
            return node.get_parameter(name).get_parameter_value().double_value

        return cls(
            image_topic=s("image_topic"),
            cmd_vel_topic=s("cmd_vel_topic"),
            arm_camera_image_topic=s("arm_camera_image_topic"),
            odom_topic=s("odom_topic"),
            current_nav_mode_topic=s("current_nav_mode_topic"),
            scan_topic=s("scan_topic"),
            send_arm_camera_image=b("send_arm_camera_image"),
            log_everything=b("log_everything"),
            simulator_mode=b("simulator_mode"),
            gemini_model=s("gemini_model"),
            gemini_thinking_level=s("gemini_thinking_level"),
            idle_turn_interval=d("idle_turn_interval"),
            supervision_turn_interval=d("supervision_turn_interval"),
            history_max_entries=i("history_max_entries"),
            history_max_image_turns=i("history_max_image_turns"),
            scan_stale_after_sec=d("scan_stale_after_sec"),
            cartesia_voice_id=s("cartesia_voice_id"),
            openai_realtime_model=s("openai_realtime_model"),
            openai_realtime_url=s("openai_realtime_url"),
            openai_transcribe_model=s("openai_transcribe_model"),
        )

#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ROS 2 node that reports simulator usage to the Innate cloud.

The sim counterpart to :mod:`innate_logger.logger_node`, which it deliberately
does not share code with: that node reports battery and diagnostics a sim does
not have, requires a service key most sim users do not have, and blocks on
auth at startup because a booting robot will get a network eventually. Here,
offline is a place users live.

Nothing in this node blocks the executor. Callbacks only ever enqueue; all
network I/O belongs to :class:`SimTelemetryUploader`'s thread.
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from pathlib import Path

import psutil
import rclpy
from dotenv import find_dotenv, load_dotenv
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String

from innate_logger.sim_client import SimAuth, SimTelemetryUploader

DEFAULT_TELEMETRY_URL = "https://logs.svc.innate.bot"
DEFAULT_AUTH_ISSUER_URL = "https://auth-v1.svc.innate.bot"

# Nothing a sim reports changes on a robot's 5 s timescale, and a slower beat
# keeps an idle sim from dominating the events table.
HEARTBEAT_INTERVAL_S = 30.0

# /odom's nominal rate in the sim driver (mars_sim_driver.node.ODOM_HZ). The
# ratio of observed to nominal is the sim's real-time factor: when a machine
# cannot keep up, the driver's timers slip and this falls away from 1.0. It is
# the closest thing the sim has to a battery gauge.
ODOM_NOMINAL_HZ = 30.0

# One teleop event per this many seconds. A joystick publishes at 20 Hz and the
# funnel only asks whether the user ever drove the robot.
TELEOP_EVENT_INTERVAL_S = 300.0

# Where unsent events wait. Under ~/.ros, which the launcher already persists
# to the host, so a container restart does not lose them.
SPOOL_PATH = Path.home() / ".ros" / "innate-sim-telemetry.jsonl"


def _env_flag_disabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"0", "false", "no", "off"}


class SimLoggerNode(Node):
    """Reports that this simulator install is alive, and what it is doing."""

    def __init__(self) -> None:
        super().__init__("sim_logger_node")

        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path)

        if _env_flag_disabled("INNATE_TELEMETRY"):
            self.get_logger().info("Sim telemetry disabled by INNATE_TELEMETRY")
            self._uploader = None
            return

        install_id = os.getenv("INNATE_SIM_INSTALL_ID", "").strip()
        if not install_id:
            # Set by the launcher for every run. Absent means someone started
            # the stack by hand, and inventing one here would register a new
            # install on every launch.
            self.get_logger().info("No INNATE_SIM_INSTALL_ID — sim telemetry disabled")
            self._uploader = None
            return

        self._session_id = str(uuid.uuid4())
        self._uploader = SimTelemetryUploader(
            telemetry_url=os.getenv("TELEMETRY_URL", DEFAULT_TELEMETRY_URL),
            auth=SimAuth(
                issuer_url=os.getenv("INNATE_AUTH_URL", DEFAULT_AUTH_ISSUER_URL),
                install_id=install_id,
                service_key=os.getenv("INNATE_SERVICE_KEY", "").strip(),
            ),
            spool_path=SPOOL_PATH,
            identity={
                "install_id": install_id,
                "device_hash": os.getenv("INNATE_SIM_DEVICE_HASH", "").strip() or None,
                "session_id": self._session_id,
                "source": "sim",
            },
        )
        self._uploader.start()

        self._odom_count = 0
        self._last_teleop_event = 0.0
        self._started_at = self.get_clock().now()

        self.create_subscription(Odometry, "/odom", self._on_odom, _best_effort_qos())
        self.create_subscription(Twist, "/cmd_vel_teleop", self._on_teleop, _best_effort_qos())
        self.create_subscription(String, "/brain/set_directive", self._on_directive, 10)
        self.create_subscription(String, "/brain/skill_status_update", self._on_skill_status, 10)
        # Challenge outcomes, published by the world server over rosbridge
        # (mars_sim_driver.challenges.TelemetryEventBridge).
        self.create_subscription(String, "/sim/telemetry_event", self._on_sim_event, 10)

        self.create_timer(HEARTBEAT_INTERVAL_S, self._on_heartbeat)

        self._uploader.event("sim_started", self._build_info())
        self.get_logger().info(f"Sim telemetry started (install {install_id[:8]}…)")

    # ── Callbacks ───────────────────────────────────────────────────

    def _on_odom(self, _msg: Odometry) -> None:
        self._odom_count += 1

    def _on_teleop(self, _msg: Twist) -> None:
        now = self._monotonic()
        if now - self._last_teleop_event < TELEOP_EVENT_INTERVAL_S:
            return
        self._last_teleop_event = now
        self._emit("teleop_used")

    def _on_directive(self, msg: String) -> None:
        # The directive text is the user's own prompt, so only its presence and
        # length are reported.
        self._emit("agent_started", {"directive_chars": len(msg.data)})

    def _on_skill_status(self, msg: String) -> None:
        try:
            status = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        if status.get("status") not in {"started", "running"}:
            return
        self._emit("skill_invoked", {"skill": str(status.get("skill") or status.get("name") or "")})

    def _on_sim_event(self, msg: String) -> None:
        try:
            frame = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        name = str(frame.get("event") or "")
        if not name:
            return
        payload = frame.get("payload")
        self._emit(name, payload if isinstance(payload, dict) else None)

    def _on_heartbeat(self) -> None:
        if self._uploader is None:
            return

        observed_hz = self._odom_count / HEARTBEAT_INTERVAL_S
        self._odom_count = 0

        self._uploader.heartbeat(
            {
                **self._build_info(),
                "cpu_usage": psutil.cpu_percent(interval=None),
                "ram_gb": round(psutil.virtual_memory().total / 1024**3, 1),
                "cpu_count": psutil.cpu_count(),
                # None rather than 0.0 before /odom has ever arrived, so an
                # unstarted sim is not indistinguishable from a stalled one.
                "real_time_factor": (
                    round(min(observed_hz / ODOM_NOMINAL_HZ, 1.0), 3) if observed_hz else None
                ),
                "uptime_s": round(
                    (self.get_clock().now() - self._started_at).nanoseconds / 1e9, 1
                ),
            }
        )

    # ── Helpers ─────────────────────────────────────────────────────

    def _build_info(self) -> dict[str, object]:
        """Which build of the sim this is, as resolved by the host launcher.

        None of it can be worked out in here: the container has no .git, and
        the image tag is a compose-level fact.
        """
        return {
            "commit": os.getenv("INNATE_SIM_COMMIT", "").strip() or None,
            "image_ref": os.getenv("INNATE_SIM_IMAGE_REF", "").strip() or None,
            "launcher_version": os.getenv("INNATE_SIM_LAUNCHER_VERSION", "").strip() or None,
            "platform": os.getenv("INNATE_SIM_PLATFORM", "").strip() or platform.system().lower(),
            "platform_release": os.getenv("INNATE_SIM_PLATFORM_RELEASE", "").strip() or None,
            "brain_backend": os.getenv("INNATE_SIM_BRAIN_BACKEND", "").strip() or None,
        }

    def _emit(self, name: str, payload: dict[str, object] | None = None) -> None:
        if self._uploader is not None:
            self._uploader.event(name, payload)

    def _monotonic(self) -> float:
        return self.get_clock().now().nanoseconds / 1e9

    def shutdown(self) -> None:
        if self._uploader is None:
            return
        self._uploader.event("sim_stopped")
        self._uploader.stop()


def _best_effort_qos() -> QoSProfile:
    """Match the sim driver's sensor streams, and never queue a backlog.

    Counting /odom for the real-time factor means the arrival rate must be the
    true one — a reliable queue would replay a burst after any hiccup and hide
    exactly the stall being measured.
    """
    return QoSProfile(
        reliability=QoSReliabilityPolicy.BEST_EFFORT,
        durability=QoSDurabilityPolicy.VOLATILE,
        history=QoSHistoryPolicy.KEEP_LAST,
        depth=1,
    )


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = SimLoggerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()

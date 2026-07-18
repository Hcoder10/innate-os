#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Teleop bag recorder — full-fidelity capture for offline 3D reconstruction.

Wraps `ros2 bag record` in a start/stop service pair so the webapp's teleop
record button can drive it. Each recording lands in
$INNATE_OS_ROOT/data/recordings/teleop_<timestamp>/ as a standard rosbag2.

Topic choices (RECORD_TOPICS):
  - Raw synchronized stereo pair at the full camera rate plus both camera_info
    streams — the reconstruction inputs. The JPEG /compressed variants are
    skipped (redundant with raw, and left runs at half rate).
  - Depth/points are NOT recorded: the depth estimator computes them lazily,
    so subscribing would add GPU load during teleop, and they are recomputable
    offline from the stereo pair + calibration.
  - /tf + /tf_static + /joint_states give the live kinematic chain (head tilt
    and arm pose move two of the cameras); /odom, /scan, /amcl_pose,
    /mapping_pose, /map and /cmd_vel cover ego-motion in every nav mode.

Services (std_srvs/Trigger):
  /recording/start  -> success + bag path (fails if already recording)
  /recording/stop   -> SIGINT the recorder, wait for finalize, report size

Status: /recording/status, std_msgs/String JSON
  {recording, path, started_at, size_bytes, error} republished every 2 s
  (heartbeat, mirroring /brain/agent_status) so any client — including one
  that connects mid-recording — converges on the true state.
"""

import json
import os
import signal
import subprocess
import time
from datetime import datetime

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger

RECORD_TOPICS = [
    # Cameras (3): raw stereo pair (synchronized, full 15 fps) + arm camera.
    "/mars/main_camera/left/image_raw",
    "/mars/main_camera/right/image_raw",
    "/mars/arm/image_raw",
    "/mars/main_camera/left/camera_info",
    "/mars/main_camera/right/camera_info",
    # Ego-motion / kinematics.
    "/odom",
    "/tf",
    "/tf_static",
    "/joint_states",
    "/mars/arm/state",
    "/mars/head/current_position",
    "/cmd_vel",
    # Nav context.
    "/scan",
    "/amcl_pose",
    "/mapping_pose",
    "/map",
    "/nav/current_mode",
]

STATUS_PERIOD_S = 2.0
# `ros2 bag record` finalizes (flushes cache, writes metadata.yaml) on SIGINT;
# raw stereo recordings can take a few seconds to drain.
STOP_FINALIZE_TIMEOUT_S = 20.0


class RecordingManager(Node):
    def __init__(self):
        super().__init__("recording_manager")

        root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
        self.recordings_dir = os.path.join(root, "data", "recordings")

        self.proc = None
        self.bag_path = None
        self.started_at = None
        self.last_error = ""

        self.status_pub = self.create_publisher(String, "/recording/status", 10)
        self.create_service(Trigger, "/recording/start", self.on_start)
        self.create_service(Trigger, "/recording/stop", self.on_stop)
        self.create_timer(STATUS_PERIOD_S, self.on_status_tick)

        self.get_logger().info(f"Recording manager ready — bags go to {self.recordings_dir}")

    # ---- services -----------------------------------------------------------

    def on_start(self, _request, response):
        if self.proc is not None:
            response.success = False
            response.message = f"Already recording to {self.bag_path}"
            return response

        os.makedirs(self.recordings_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bag_path = os.path.join(self.recordings_dir, f"teleop_{stamp}")

        cmd = ["ros2", "bag", "record", *RECORD_TOPICS, "-o", bag_path]
        try:
            # Own process group so stop can SIGINT `ros2 bag` and its child
            # together without touching this node.
            proc = subprocess.Popen(cmd, start_new_session=True)
        except OSError as e:
            self.last_error = f"Failed to launch ros2 bag record: {e}"
            self.get_logger().error(self.last_error)
            response.success = False
            response.message = self.last_error
            return response

        # An immediate death (bad CLI env, unwritable dir) beats reporting a
        # phantom recording; output goes to this pane's console for diagnosis.
        time.sleep(0.5)
        if proc.poll() is not None:
            self.last_error = f"ros2 bag record exited immediately (code {proc.returncode}) — see console log"
            self.get_logger().error(self.last_error)
            response.success = False
            response.message = self.last_error
            return response

        self.proc = proc
        self.bag_path = bag_path
        self.started_at = time.time()
        self.last_error = ""
        self.get_logger().info(f"Recording {len(RECORD_TOPICS)} topics to {bag_path}")
        self.publish_status()

        response.success = True
        response.message = bag_path
        return response

    def on_stop(self, _request, response):
        if self.proc is None:
            response.success = False
            response.message = "Not recording"
            return response

        path = self.bag_path
        self.stop_recording()
        size = dir_size(path)
        self.publish_status()

        response.success = True
        response.message = f"Saved {path} ({format_size(size)})"
        return response

    # ---- recorder process ---------------------------------------------------

    def stop_recording(self):
        """SIGINT the recorder's process group and wait for bag finalize."""
        proc, self.proc = self.proc, None
        self.bag_path, self.started_at = None, None
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            proc.wait(timeout=STOP_FINALIZE_TIMEOUT_S)
        except (ProcessLookupError, PermissionError):
            pass
        except subprocess.TimeoutExpired:
            self.get_logger().warning("ros2 bag record did not finalize in time; killing it")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

    def on_status_tick(self):
        # A recorder that died on its own (disk full, crash) must not leave the
        # UI showing a live recording.
        if self.proc is not None and self.proc.poll() is not None:
            self.last_error = f"ros2 bag record exited unexpectedly (code {self.proc.returncode})"
            self.get_logger().error(self.last_error)
            self.proc = None
            self.bag_path, self.started_at = None, None
        self.publish_status()

    def publish_status(self):
        recording = self.proc is not None
        status = {
            "recording": recording,
            "path": self.bag_path if recording else None,
            "started_at": self.started_at if recording else None,
            "size_bytes": dir_size(self.bag_path) if recording else 0,
            "error": self.last_error,
        }
        self.status_pub.publish(String(data=json.dumps(status)))

    def destroy_node(self):
        # Never orphan a recorder — finalize the bag on node shutdown.
        self.stop_recording()
        super().destroy_node()


def dir_size(path):
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    for entry in os.scandir(path):
        if entry.is_file(follow_symlinks=False):
            total += entry.stat().st_size
    return total


def format_size(size_bytes):
    if size_bytes >= 1 << 30:
        return f"{size_bytes / (1 << 30):.1f} GB"
    return f"{size_bytes / (1 << 20):.0f} MB"


def main():
    rclpy.init()
    node = RecordingManager()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()

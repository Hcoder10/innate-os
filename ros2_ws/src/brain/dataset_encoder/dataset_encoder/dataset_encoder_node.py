#!/usr/bin/env python3
"""dataset_encoder — background episode → H.264 MP4 encoder.

Freshly recorded episodes are stored as raw frames in HDF5. This node converts
them to H.264 MP4 (+ stripped HDF5 + metadata flip) the moment they're saved, so
both apps can replay episodes as ordinary seekable video files and a later
cloud-sync becomes a pure upload. It reuses the *exact* converter that sync runs
(``training_client.src.episode_converter.convert_episodes_to_h264``), which is
idempotent and resumable — so a power-off mid-encode self-heals on the next pass.

Hiccup safety: encoding is gated. It pauses (between work items) whenever the
robot is recording/replaying, a skill/policy is executing, or a live WebRTC
stream is up — and it runs at a high ``nice`` level with idle I/O and few
threads. One episode-camera is encoded at a time, never in parallel.

Single-threaded by design: a timer advances the converter generator one work
item per tick, so subscription callbacks (gating) and the encode service stay on
one executor with no locking. Each item is a few seconds of gst encoding.
"""

import json
import os
from collections import deque
from pathlib import Path

import rclpy
from brain_messages.msg import EncodeStatus, RecorderStatus
from brain_messages.srv import EncodeEpisode
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile
from std_msgs.msg import Bool, String

# Reused, idempotent converter — same code path the cloud-sync runs.
from training_client.src.episode_converter import (
    DATASET_METADATA,
    convert_episodes_to_h264,
)
from training_client.src.types import ProgressStage

DATA_SUBDIR = "data"
# Recorder status values that mean a capture/replay session is in progress.
RECORDER_BUSY = {"active", "stopped"}
# Substrings that mark a skill_status_update as "a skill is running".
SKILL_RUNNING_HINTS = ("run", "execut", "active", "start", "play", "progress")
SKILL_IDLE_HINTS = ("idle", "done", "complete", "finish", "stopp", "cancel", "error", "fail")


class DatasetEncoder(Node):
    def __init__(self):
        super().__init__("dataset_encoder")

        default_root = os.path.join(
            os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os")),
            "workspace",
            "custom_skills",
        )
        self.declare_parameter("skills_root", default_root)
        self.declare_parameter("nice_level", 15)
        self.declare_parameter("encode_threads", 2)
        self.declare_parameter("idle_io", True)
        self.declare_parameter("tick_period_sec", 0.5)
        self.declare_parameter("scan_period_sec", 60.0)
        self.declare_parameter("skill_stale_sec", 15.0)

        self.skills_root = os.path.expanduser(self.get_parameter("skills_root").value)
        self.nice_level = int(self.get_parameter("nice_level").value)
        self.encode_threads = int(self.get_parameter("encode_threads").value)
        self.idle_io = bool(self.get_parameter("idle_io").value)
        self.skill_stale_sec = float(self.get_parameter("skill_stale_sec").value)

        # Work queue of skill task_directories awaiting conversion (deduped).
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()

        # Active conversion generator + bookkeeping for the dir being processed.
        self._gen = None
        self._current_dir = ""
        self._cur_index = 0
        self._cur_total = 0
        self._cur_file = ""

        # Gating inputs.
        self._recording = False
        self._skill_active_until = 0.0
        self._webrtc_active = False
        self._last_gated_pub = False

        # Latched status so a late subscriber (the apps) gets current state.
        latched = QoSProfile(
            depth=1,
            history=QoSHistoryPolicy.KEEP_LAST,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._status_pub = self.create_publisher(EncodeStatus, "/datasets/encode_status", latched)

        self.create_subscription(RecorderStatus, "/brain/recorder/status", self._on_recorder, 10)
        self.create_subscription(String, "/brain/skill_status_update", self._on_skill_status, 10)
        # Volatile to match the streamer's publisher (it runs intra-process, which
        # forbids transient_local). We subscribe at startup and follow transitions.
        self.create_subscription(Bool, "/webrtc/status", self._on_webrtc, 10)

        self.create_service(EncodeEpisode, "/datasets/encode_episode", self._on_encode_request)

        tick = float(self.get_parameter("tick_period_sec").value)
        self.create_timer(tick, self._tick)
        self.create_timer(float(self.get_parameter("scan_period_sec").value), self._scan_skills)

        self._scan_skills()  # catch up on anything left un-encoded across reboots
        self._publish_idle()
        self.get_logger().info(
            f"dataset_encoder up; root={self.skills_root}, nice={self.nice_level}, "
            f"threads={self.encode_threads}, idle_io={self.idle_io}"
        )

    # ---- gating ----------------------------------------------------------
    def _now(self) -> float:
        return self.get_clock().now().nanoseconds / 1e9

    def _skill_active(self) -> bool:
        return self._now() < self._skill_active_until

    def _gated(self) -> bool:
        # Gating disabled: encoding runs continuously and stays out of the way
        # purely via low priority (nice=15, reduced threads, idle I/O). Pausing
        # on recording/teleop/skill left new episodes stuck un-encoded whenever
        # a live view was open. The recorder subscription still drives
        # enqueue-on-save; only the pause is gone. (Restore by returning the old
        # `self._recording or self._webrtc_active or self._skill_active()`.)
        return False

    def _on_recorder(self, msg: RecorderStatus):
        self._recording = msg.status in RECORDER_BUSY
        if msg.status == "saved" and msg.task_directory:
            self._enqueue(msg.task_directory)

    def _on_skill_status(self, msg: String):
        try:
            data = json.loads(msg.data) if msg.data.strip() else {}
        except (ValueError, TypeError):
            data = {}
        blob = " ".join(str(data.get(k, "")) for k in ("status", "state", "phase", "event", "type")).lower()
        if not blob:
            blob = str(msg.data).lower()
        if any(h in blob for h in SKILL_IDLE_HINTS):
            self._skill_active_until = 0.0
        elif any(h in blob for h in SKILL_RUNNING_HINTS):
            self._skill_active_until = self._now() + self.skill_stale_sec

    def _on_webrtc(self, msg: Bool):
        self._webrtc_active = bool(msg.data)

    # ---- queue management ------------------------------------------------
    def _enqueue(self, task_directory: str, front: bool = False):
        task_directory = os.path.abspath(os.path.expanduser(task_directory))
        if task_directory == self._current_dir or task_directory in self._queued:
            return
        if not self._needs_conversion(task_directory):
            return
        self._queued.add(task_directory)
        if front:
            self._queue.appendleft(task_directory)
        else:
            self._queue.append(task_directory)
        self.get_logger().info(f"queued for encode: {task_directory} (depth={len(self._queue)})")

    def _needs_conversion(self, task_directory: str) -> bool:
        """Cheap JSON-only check: any episode without video_files / not h264."""
        meta_path = os.path.join(task_directory, DATA_SUBDIR, DATASET_METADATA)
        try:
            with open(meta_path) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return False
        episodes = meta.get("episodes", [])
        if not episodes:
            return False
        if meta.get("dataset_type") != "h264":
            return True
        return any(not ep.get("video_files") for ep in episodes if ep.get("file_name"))

    def _scan_skills(self):
        try:
            entries = sorted(os.scandir(self.skills_root), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                self._enqueue(entry.path)

    # ---- service ---------------------------------------------------------
    def _on_encode_request(self, request, response):
        task_dir = os.path.abspath(os.path.expanduser(request.task_directory))
        if not os.path.isdir(os.path.join(task_dir, DATA_SUBDIR)):
            response.success = False
            response.message = f"No data/ directory under {task_dir}"
            return response
        if not self._needs_conversion(task_dir):
            response.success = True
            response.message = "Already encoded; nothing to do"
            return response
        self._enqueue(task_dir, front=True)
        response.success = True
        response.message = f"Queued {task_dir} for encoding"
        return response

    # ---- worker (timer advances one work item per tick) ------------------
    def _tick(self):
        if self._gen is None:
            if self._gated() or not self._queue:
                return
            self._current_dir = self._queue.popleft()
            self._queued.discard(self._current_dir)
            data_dir = os.path.join(self._current_dir, DATA_SUBDIR)
            self._gen = convert_episodes_to_h264(
                Path(data_dir),
                nice_level=self.nice_level,
                threads=self.encode_threads,
                idle_io=self.idle_io,
            )
            self._cur_index = self._cur_total = 0
            self._cur_file = ""

        if self._gated():
            self._publish_gated()
            return

        try:
            update = next(self._gen)
        except StopIteration:
            self._publish(stage="done", message="Encoding complete")
            self._reset_current()
            return
        except Exception as exc:  # converter raised mid-encode
            self.get_logger().error(f"encode failed for {self._current_dir}: {exc}")
            self._publish(stage="error", message=str(exc), error=str(exc))
            self._reset_current()
            return

        fp = update.file_progress
        if fp is not None:
            self._cur_index = fp.index
            self._cur_total = fp.total
            self._cur_file = fp.filename
        stage = "error" if update.stage == ProgressStage.ERROR else "encoding"
        self._publish(stage=stage, message=update.message, error=update.error or "")

    def _reset_current(self):
        self._gen = None
        self._current_dir = ""
        self._cur_index = self._cur_total = 0
        self._cur_file = ""

    # ---- status publishing ----------------------------------------------
    def _publish(self, *, stage, message="", error=""):
        # Reset the gated-transition latch whenever we emit a non-gated update.
        if stage != "gated":
            self._last_gated_pub = False
        msg = EncodeStatus()
        msg.task_directory = self._current_dir
        msg.stage = stage
        msg.current_file = self._cur_file
        msg.current_index = self._cur_index
        msg.total = self._cur_total
        msg.progress = (self._cur_index / self._cur_total) if self._cur_total else 0.0
        msg.message = message
        msg.error = error
        msg.queue_depth = len(self._queue)
        self._status_pub.publish(msg)

    def _publish_idle(self):
        self._reset_current()
        self._publish(stage="idle", message="idle")

    def _publish_gated(self):
        # Emit a gated update only on transition, to avoid spamming the topic.
        if not self._last_gated_pub:
            self._publish(stage="gated", message="paused — robot busy")
            self._last_gated_pub = True


def main():
    rclpy.init()
    node = DatasetEncoder()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()

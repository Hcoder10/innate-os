#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""profile_recorder — persist the per-step inference profile alongside episodes.

While the recorder has an episode open, the inference server's per-step profile
stream (/brain/manipulation/inference_profile: timings, engine_ran, progress,
jerk, disagreement, full raw action vector) only feeds live charts and is lost
when the run ends. This node tees it to disk: it follows /brain/recorder/status
(the same hook dataset_encoder rides), buffers the stream while an episode is
recording, and on save writes ``data/episode_<id>_profile.jsonl`` next to the
episode's HDF5 — so every saved rollout carries its full profiling trace for
offline evaluation (e.g. replaying auto-stop detectors over real runs).

The profile topic is subscriber-gated in the inference server (extra GPU sync +
JSON encode only run when someone listens), so this node subscribes only while
an episode is actually recording — profiling stays free when idle.

File format: line 1 is a context record (``{"type": "context", ...}`` with the
skill that was running and the episode id); every following line is one profile
sample verbatim as published.
"""

import json
import os
from collections import deque

import rclpy
from brain_messages.msg import RecorderStatus
from rclpy.node import Node
from std_msgs.msg import String

DATA_SUBDIR = "data"
DATASET_METADATA = "dataset_metadata.json"
INFERENCE_PROFILE_TOPIC = "/brain/manipulation/inference_profile"
SKILL_STATUS_TOPIC = "/brain/skill_status_update"
# ~66 min at 25 Hz — far beyond any episode (recorder caps at max_timesteps),
# purely a memory guard against a wedged recorder state.
MAX_SAMPLES = 100_000


class ProfileRecorder(Node):
    def __init__(self):
        super().__init__("profile_recorder")

        self._samples: deque[str] = deque(maxlen=MAX_SAMPLES)
        self._task_dir = ""
        self._profile_sub = None
        # Last skill reported "running" — recorded as context in the output so a
        # profile file is attributable to a skill/checkpoint without joining
        # against other logs. Best-effort: empty if no skill status was seen.
        self._running_skill: dict = {}

        self.create_subscription(RecorderStatus, "/brain/recorder/status", self._on_recorder, 10)
        self.create_subscription(String, SKILL_STATUS_TOPIC, self._on_skill_status, 10)
        self.get_logger().info("profile_recorder up; waiting for recorder episodes")

    # ---- inputs ----------------------------------------------------------
    def _on_skill_status(self, msg: String):
        try:
            payload = json.loads(msg.data)
        except ValueError:
            return
        if payload.get("status") == "running":
            self._running_skill = {
                k: payload[k] for k in ("skill_name", "skill_id", "primitive_name") if payload.get(k)
            }

    def _on_profile(self, msg: String):
        self._samples.append(msg.data)

    def _on_recorder(self, msg: RecorderStatus):
        # "stopped" (a paused episode, awaiting save/cancel) is deliberately left
        # unhandled: it's reachable from teleop's stop_episode, which never has
        # profile samples to lose, and staying subscribed through it means a
        # rollout that gets paused mid-run keeps its trace gap-free instead of
        # this node guessing whether to hold or drop the buffer.
        recording = msg.status == "active" and msg.episode_number != ""
        if recording:
            self._start_capture(msg.task_directory)
        elif msg.status == "saved":
            self._write_profile(msg.task_directory or self._task_dir)
            self._stop_capture()
        elif msg.status in ("cancelled", "idle"):
            self._stop_capture()

    # ---- capture lifecycle -----------------------------------------------
    def _start_capture(self, task_dir: str):
        if self._profile_sub is None:
            self._samples.clear()
            self._profile_sub = self.create_subscription(String, INFERENCE_PROFILE_TOPIC, self._on_profile, 50)
            self.get_logger().info(f"capturing inference profile for episode in {task_dir}")
        self._task_dir = task_dir or self._task_dir

    def _stop_capture(self):
        if self._profile_sub is not None:
            self.destroy_subscription(self._profile_sub)
            self._profile_sub = None
        self._samples.clear()
        self._task_dir = ""

    # ---- output ------------------------------------------------------------
    def _write_profile(self, task_dir: str):
        if not task_dir or not self._samples:
            return
        try:
            # The recorder registers the episode in dataset_metadata.json (atomic
            # write) *before* publishing "saved", so the last entry is this
            # episode. RecorderStatus.episode_number is a running count, not the
            # slot id — don't trust it for the filename.
            with open(os.path.join(task_dir, DATA_SUBDIR, DATASET_METADATA)) as fh:
                episodes = json.load(fh).get("episodes", [])
            if not episodes:
                raise ValueError("no episodes in dataset metadata after save")
            episode_id = int(episodes[-1]["episode_id"])

            context = {
                "type": "context",
                "episode_id": episode_id,
                "task_directory": task_dir,
                "sample_count": len(self._samples),
                "skill": self._running_skill,
                "topic": INFERENCE_PROFILE_TOPIC,
            }
            path = os.path.join(task_dir, DATA_SUBDIR, f"episode_{episode_id}_profile.jsonl")
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(json.dumps(context) + "\n")
                fh.writelines(line + "\n" for line in self._samples)
            os.replace(tmp, path)
            self.get_logger().info(f"wrote {len(self._samples)} profile samples to {path}")
        except (OSError, ValueError, KeyError) as exc:
            # Profile persistence must never interfere with the episode save.
            self.get_logger().error(f"failed to write profile for {task_dir}: {exc}")


def main():
    rclpy.init()
    node = ProfileRecorder()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Operator-in-the-loop rollout evaluation for learned skills.

Runs a learned skill N times, recording every rollout as a labeled episode in
an evaluation task directory. Each trial:

1. prompts the operator to reset the scene,
2. starts a recorder episode with policy-head capture on (the trailing two
   /action columns hold the policy's live progress/termination head, and the
   episode is tagged ``action_head_source: "policy"``),
3. executes the skill via the /behavior/execute action,
4. saves the episode and asks the operator for a success/failure label
   (stored via /brain/recorder/set_episode_outcome) plus an optional
   free-text failure note (stored in ``eval_log.jsonl`` in the eval dir).

The result is an ordinary recorder dataset — episode_N.h5 + auto-encoded MP4s +
dataset_metadata.json — whose outcome labels give task success rate and whose
real head columns feed offline auto-stop evaluation.

Usage (on the robot, with recorder_node and manipulation_server running)::

    python3 -m manipulation.rollout_eval \
        --skill-dir ~/innate-os/workspace/custom_skills/pick-up-socks \
        --eval-name pick-up-socks-eval \
        --trials 10 [--checkpoint act_policy_step_135000.pth]

``--eval-dir`` reuses an existing eval task directory instead of creating one;
``--checkpoint`` overrides ``execution.checkpoint`` for A/B between training
steps without editing the skill's metadata.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import rclpy
from brain_messages.action import ExecuteBehavior
from brain_messages.srv import ActivateManipulationTask, CreatePhysicalSkill, GetTaskMetadata, SetEpisodeOutcome
from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
from rcl_interfaces.srv import SetParameters
from rclpy.action import ActionClient
from rclpy.node import Node
from std_srvs.srv import Trigger

RECORDER = "/brain/recorder"


class RolloutEval(Node):
    def __init__(self):
        super().__init__("rollout_eval")
        self._behavior_client = ActionClient(self, ExecuteBehavior, "/behavior/execute")

    # --- service plumbing ----------------------------------------------------

    def call(self, srv_type, name, request, timeout=10.0):
        client = self.create_client(srv_type, name)
        try:
            if not client.wait_for_service(timeout_sec=timeout):
                raise RuntimeError(f"service {name} not available")
            future = client.call_async(request)
            rclpy.spin_until_future_complete(self, future, timeout_sec=timeout)
            if not future.done():
                raise RuntimeError(f"service {name} timed out")
            return future.result()
        finally:
            self.destroy_client(client)

    def trigger(self, name):
        result = self.call(Trigger, name, Trigger.Request())
        if not result.success:
            raise RuntimeError(f"{name}: {result.message}")
        return result

    def set_recorder_capture(self, enabled: bool):
        param = Parameter(
            name="capture_policy_head",
            value=ParameterValue(type=ParameterType.PARAMETER_BOOL, bool_value=enabled),
        )
        result = self.call(SetParameters, "/recorder_node/set_parameters", SetParameters.Request(parameters=[param]))
        if not result.results[0].successful:
            raise RuntimeError(f"failed to set capture_policy_head={enabled}: {result.results[0].reason}")

    # --- eval steps -----------------------------------------------------------

    def resolve_eval_dir(self, eval_dir: str | None, eval_name: str | None) -> str:
        if eval_dir:
            if not os.path.isfile(os.path.join(eval_dir, "metadata.json")):
                raise RuntimeError(f"{eval_dir} has no metadata.json; create it with /brain/create_physical_skill")
            return eval_dir
        result = self.call(
            CreatePhysicalSkill, "/brain/create_physical_skill", CreatePhysicalSkill.Request(name=eval_name, kind="")
        )
        if not result.success:
            raise RuntimeError(f"create_physical_skill: {result.message}")
        return result.skill_directory

    def execute_behavior(self, skill_dir: str, behavior_config: str) -> tuple[bool, str]:
        """Send the skill to the behavior server and wait for it to finish."""
        if not self._behavior_client.wait_for_server(timeout_sec=10.0):
            raise RuntimeError("behavior server (/behavior/execute) not available")
        goal = ExecuteBehavior.Goal(skill_dir=skill_dir, behavior_config=behavior_config)
        send_future = self._behavior_client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, send_future, timeout_sec=10.0)
        handle = send_future.result()
        if handle is None or not handle.accepted:
            raise RuntimeError("behavior goal rejected")
        result_future = handle.get_result_async()
        try:
            rclpy.spin_until_future_complete(self, result_future)  # bounded by the skill's own duration cap
        except KeyboardInterrupt:
            # Don't leave the policy driving the robot after the operator bails.
            cancel_future = handle.cancel_goal_async()
            rclpy.spin_until_future_complete(self, cancel_future, timeout_sec=5.0)
            raise
        result = result_future.result().result
        return result.success, result.message

    def last_episode_id(self, eval_dir: str) -> int:
        result = self.call(
            GetTaskMetadata, f"{RECORDER}/get_task_metadata", GetTaskMetadata.Request(task_directory=eval_dir)
        )
        if not result.success:
            raise RuntimeError(f"get_task_metadata: {result.message}")
        episodes = json.loads(result.json_metadata).get("episodes", [])
        if not episodes:
            raise RuntimeError("no episodes in eval task metadata after save")
        return int(episodes[-1]["episode_id"])

    def label_episode(self, eval_dir: str, episode_id: int, outcome: str):
        result = self.call(
            SetEpisodeOutcome,
            f"{RECORDER}/set_episode_outcome",
            SetEpisodeOutcome.Request(task_directory=eval_dir, episode_id=episode_id, outcome=outcome),
        )
        if not result.success:
            raise RuntimeError(f"set_episode_outcome: {result.message}")


def ask(prompt: str) -> str:
    return input(prompt).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skill-dir", required=True, help="Skill directory of the learned skill under test")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--eval-dir", help="Existing eval task directory to record into")
    group.add_argument("--eval-name", help="Create a new eval task directory with this name")
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--checkpoint", help="Override execution.checkpoint (A/B between training steps)")
    args = parser.parse_args()

    skill_dir = os.path.abspath(os.path.expanduser(args.skill_dir))
    with open(os.path.join(skill_dir, "metadata.json")) as f:
        metadata = json.load(f)
    if metadata.get("type") != "learned":
        sys.exit(f"--skill-dir must be a learned skill, got type={metadata.get('type')!r}")
    if args.checkpoint:
        metadata["execution"]["checkpoint"] = args.checkpoint
    behavior_config = json.dumps(metadata)
    checkpoint = metadata["execution"].get("checkpoint")

    rclpy.init()
    node = RolloutEval()
    results = []
    eval_dir = None
    try:
        eval_dir = node.resolve_eval_dir(
            os.path.abspath(os.path.expanduser(args.eval_dir)) if args.eval_dir else None, args.eval_name
        )
        log_path = os.path.join(eval_dir, "eval_log.jsonl")
        print(f"Recording rollouts into {eval_dir} (labels also in {log_path})")

        node.set_recorder_capture(True)
        activate = node.call(
            ActivateManipulationTask,
            f"{RECORDER}/activate_physical_primitive",
            ActivateManipulationTask.Request(task_directory=eval_dir),
        )
        if not activate.success:
            raise RuntimeError("activate_physical_primitive failed")

        for trial in range(1, args.trials + 1):
            ask(f"\n[trial {trial}/{args.trials}] Reset the scene, then press Enter (Ctrl-C to finish early)... ")
            node.trigger(f"{RECORDER}/new_episode")
            try:
                ok, message = node.execute_behavior(skill_dir, behavior_config)
            except (Exception, KeyboardInterrupt):
                node.trigger(f"{RECORDER}/cancel_episode")
                raise
            if not ok:
                # The behavior itself failed to run (sensors, config) — not a
                # policy failure. Discard the episode so it can't skew the eval.
                print(f"  behavior errored ({message}); episode discarded, not counted")
                node.trigger(f"{RECORDER}/cancel_episode")
                continue
            node.trigger(f"{RECORDER}/save_episode")
            episode_id = node.last_episode_id(eval_dir)

            success = ask("  task successful? [y/n] ").lower().startswith("y")
            note = "" if success else ask("  failure note (optional): ")
            node.label_episode(eval_dir, episode_id, "success" if success else "failure")

            record = {
                "episode_id": episode_id,
                "outcome": "success" if success else "failure",
                "note": note,
                "skill_dir": skill_dir,
                "checkpoint": checkpoint,
                "behavior_message": message,
                "t": time.time(),
            }
            with open(log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
            results.append(record)
            print(f"  episode {episode_id}: {record['outcome']}")
    except KeyboardInterrupt:
        print("\nInterrupted; finishing up.")
    finally:
        for cleanup in (lambda: node.trigger(f"{RECORDER}/end_task"), lambda: node.set_recorder_capture(False)):
            try:
                cleanup()
            except Exception as e:  # noqa: BLE001 - best-effort teardown, report and continue
                print(f"cleanup warning: {e}")
        node.destroy_node()
        rclpy.shutdown()

    if results:
        wins = sum(r["outcome"] == "success" for r in results)
        print(f"\n=== {wins}/{len(results)} successful ({100.0 * wins / len(results):.0f}%) ===")
        print(f"checkpoint: {checkpoint}")
        for r in results:
            tag = f"  ({r['note']})" if r["note"] else ""
            print(f"  episode {r['episode_id']}: {r['outcome']}{tag}")


if __name__ == "__main__":
    main()

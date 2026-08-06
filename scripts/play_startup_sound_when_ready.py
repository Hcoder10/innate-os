#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Play the robot startup sound only after the local stack is truly ready.

The launcher starts many long-running commands at once.  Process existence is
not readiness: navigation lifecycle transitions, skills/agent discovery,
sensor bring-up, and the TensorRT startup sweep can continue well after every
tmux pane exists.  This helper waits for semantic readiness and a short stable
window before it plays the sound.

Cloud authentication is deliberately not part of the gate.  An unprovisioned
or temporarily offline robot is still locally usable and should finish booting.
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import time
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

UNCONFIGURED = 1
INACTIVE = 2
ACTIVE = 3

# Keep this matrix aligned with mars_nav/mode_manager.py.  Checking the
# persisted /nav/current_mode value alone is not sufficient: mode_manager
# publishes it before its delayed lifecycle startup begins.
LIFECYCLE_EXPECTATIONS: dict[str, dict[str, int]] = {
    "mapping": {
        "/slam_toolbox": ACTIVE,
        "/controller_server": INACTIVE,
        "/mapfree/planner_server": INACTIVE,
        "/navigation/planner_server": INACTIVE,
        "/null_map_node": UNCONFIGURED,
        "/navigation_map_server": UNCONFIGURED,
        "/navigation_grid_localizer": UNCONFIGURED,
        "/navigation_amcl": UNCONFIGURED,
        "/bt_navigator": UNCONFIGURED,
        "/behavior_server": UNCONFIGURED,
        "/velocity_smoother": UNCONFIGURED,
    },
    "mapfree": {
        "/null_map_node": ACTIVE,
        "/mapfree/planner_server": ACTIVE,
        "/controller_server": ACTIVE,
        "/bt_navigator": ACTIVE,
        "/behavior_server": ACTIVE,
        "/velocity_smoother": ACTIVE,
        "/navigation/planner_server": INACTIVE,
        "/slam_toolbox": UNCONFIGURED,
        "/navigation_map_server": UNCONFIGURED,
        "/navigation_grid_localizer": UNCONFIGURED,
        "/navigation_amcl": UNCONFIGURED,
    },
    "navigation": {
        "/navigation_map_server": ACTIVE,
        "/navigation_grid_localizer": ACTIVE,
        "/navigation_amcl": ACTIVE,
        "/mapfree/planner_server": ACTIVE,
        "/navigation/planner_server": ACTIVE,
        "/controller_server": ACTIVE,
        "/bt_navigator": ACTIVE,
        "/behavior_server": ACTIVE,
        "/velocity_smoother": ACTIVE,
        "/null_map_node": UNCONFIGURED,
        "/slam_toolbox": UNCONFIGURED,
    },
}

REQUIRED_NODES = frozenset(
    {
        "/arm_camera_driver",
        "/brain_client_node",
        "/bringup",
        "/camera_container",
        "/dataset_encoder",
        "/innate_console",
        "/innate_training",
        "/input_manager_node",
        "/kdl_ik_from_file",
        "/main_camera_driver",
        "/manipulation_server",
        "/mars_app",
        "/mars_arm",
        "/mode_manager",
        "/motor_sound",
        "/profile_recorder",
        "/recorder_node",
        "/stereo_depth_estimator",
        "/udp_leader_receiver",
        "/uninavid_node",
        "/webrtc_streamer",
    }
)

REQUIRED_SERVICES = frozenset(
    {
        "/brain/reload_primitives",
        "/calibrate",
        "/datasets/encode_episode",
        "/innate_training/get_training_status",
        "/input_manager/set_input_active",
        "/mars/arm/goto_js_v2",
        "/nav/change_mode",
        "/set_volume",
    }
)

SENSOR_TOPICS = (
    "/scan",
    "/mars/main_camera/left/image_raw/compressed",
    "/mars/arm/image_raw",
    "/mars/arm/state",
)


def lifecycle_readiness_errors(mode: str, states: dict[str, int]) -> list[str]:
    """Return lifecycle mismatches for the final navigation mode."""
    expected = LIFECYCLE_EXPECTATIONS.get(mode)
    if expected is None:
        return [f"navigation mode not final ({mode or 'unpublished'})"]

    errors = []
    for node_name, expected_state in expected.items():
        actual = states.get(node_name)
        if actual != expected_state:
            actual_label = "unreachable" if actual is None else str(actual)
            errors.append(f"{node_name}={actual_label}, expected {expected_state}")
    return errors


def wait_for_stable_readiness(
    probe: Callable[[], Iterable[str]],
    advance: Callable[[float], None],
    *,
    timeout_s: float,
    stable_s: float,
    poll_s: float,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] = lambda: False,
    log: Callable[[str], None] = print,
) -> bool:
    """Wait until ``probe`` reports no errors continuously for ``stable_s``."""
    deadline = monotonic() + timeout_s
    stable_since: float | None = None
    previous_errors: tuple[str, ...] | None = None

    while monotonic() < deadline and not cancelled():
        errors = tuple(probe())
        now = monotonic()

        if errors:
            stable_since = None
            if errors != previous_errors:
                log("Startup readiness: waiting for " + "; ".join(errors))
        else:
            if stable_since is None:
                stable_since = now
                log(f"Startup readiness: all checks passed; settling for {stable_s:g}s")
            if now - stable_since >= stable_s:
                log("Startup readiness: local stack is fully ready")
                return True

        previous_errors = errors
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        advance(min(poll_s, remaining))

    if cancelled():
        log("Startup readiness: cancelled before playback")
    else:
        suffix = "; ".join(previous_errors or ("unknown readiness failure",))
        log(f"Startup readiness: timed out; sound skipped ({suffix})")
    return False


def run_startup_sequence(
    probe: Callable[[], Iterable[str]],
    advance: Callable[[float], None],
    play: Callable[[], bool],
    *,
    timeout_s: float,
    stable_s: float,
    poll_s: float,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] = lambda: False,
    log: Callable[[str], None] = print,
) -> bool:
    """Wait for readiness, then invoke the player exactly once."""
    ready = wait_for_stable_readiness(
        probe,
        advance,
        timeout_s=timeout_s,
        stable_s=stable_s,
        poll_s=poll_s,
        monotonic=monotonic,
        cancelled=cancelled,
        log=log,
    )
    if not ready:
        return False

    try:
        return bool(play())
    except Exception as exc:  # noqa: BLE001 - playback must never restart ros-app
        log(f"Startup sound: player failed: {exc}")
        return False


def _fully_qualified_node_name(name: str, namespace: str) -> str:
    namespace = namespace.rstrip("/")
    return f"{namespace}/{name}" if namespace else f"/{name}"


def _tensor_rt_sweep_running() -> bool:
    """Detect the training node's asynchronous startup engine sweep."""
    for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            args = [arg for arg in Path(cmdline_path).read_bytes().split(b"\0") if arg]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if b"--batch" in args and any(b"manipulation.act_trt" in arg for arg in args):
            return True
    return False


def _webapp_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:  # noqa: S310 - fixed localhost URL
            response.read(1)
            return response.status == 200
    except Exception:  # noqa: BLE001 - every network failure simply means not ready yet
        return False


def _tmux_panes_ready(session: str, expected_panes: int) -> bool:
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-s",
                "-t",
                session,
                "-F",
                "#{pane_dead}",
            ],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False

    panes = result.stdout.splitlines()
    if len(panes) != expected_panes:
        return False
    return all(dead == "0" for dead in panes)


def _play_sound(sound_path: Path, player: str, log: Callable[[str], None] = print) -> bool:
    log(f"Startup sound: playing {sound_path}")
    player_env = os.environ.copy()
    player_env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        result = subprocess.run(
            [player, str(sound_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=player_env,
            check=False,
        )
    except OSError as exc:
        log(f"Startup sound: could not launch {player}: {exc}")
        return False

    if result.stderr.strip():
        log("Startup sound player output: " + result.stderr.strip())
    if result.returncode != 0:
        log(f"Startup sound: player exited with status {result.returncode}")
        return False
    log("Startup sound: playback complete")
    return True


def _runtime(args: argparse.Namespace) -> int:
    # Keep ROS imports out of module scope so the pure readiness logic can be
    # unit-tested on development machines without ROS installed.
    import rclpy
    from brain_messages.action import ExecuteBehavior, ExecuteSkill
    from innate_cloud_msgs.action import NavigateInstruction
    from lifecycle_msgs.srv import GetState
    from rclpy.action import ActionClient
    from rclpy.node import Node
    from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy, qos_profile_sensor_data
    from sensor_msgs.msg import CompressedImage, Image, JointState, LaserScan
    from std_msgs.msg import String

    class StartupProbe(Node):
        def __init__(self) -> None:
            super().__init__("startup_readiness")
            self.nav_mode = ""
            self.brain_status_seen = False
            self.sensor_topics_seen: set[str] = set()
            self.lifecycle_states: dict[str, int] = {}
            self._pending_lifecycle = {}

            latched_qos = QoSProfile(
                depth=1,
                durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
                reliability=QoSReliabilityPolicy.RELIABLE,
            )
            self._status_subscriptions = [
                self.create_subscription(String, "/nav/current_mode", self._on_nav_mode, 10),
                self.create_subscription(String, "/brain/agent_status", self._on_brain_status, latched_qos),
            ]
            sensor_types = {
                "/scan": LaserScan,
                "/mars/main_camera/left/image_raw/compressed": CompressedImage,
                "/mars/arm/image_raw": Image,
                "/mars/arm/state": JointState,
            }
            self._sensor_subscriptions = {
                topic: self.create_subscription(
                    message_type,
                    topic,
                    lambda _msg, sensor_topic=topic: self.sensor_topics_seen.add(sensor_topic),
                    qos_profile_sensor_data,
                )
                for topic, message_type in sensor_types.items()
            }
            lifecycle_nodes = sorted({name for matrix in LIFECYCLE_EXPECTATIONS.values() for name in matrix})
            self._lifecycle_clients = {
                name: self.create_client(GetState, f"{name}/get_state") for name in lifecycle_nodes
            }
            action_types = {
                "/behavior/execute": ExecuteBehavior,
                "/execute_skill": ExecuteSkill,
                "/navigate_instruction": NavigateInstruction,
            }
            # Humble does not expose Node.get_action_names_and_types().  Action
            # clients give us a direct, version-compatible server readiness
            # check without spawning the ros2 CLI on every probe.
            self._action_clients = {
                name: ActionClient(self, action_type, name) for name, action_type in action_types.items()
            }

        def _on_nav_mode(self, msg: String) -> None:
            self.nav_mode = msg.data.strip().lower()

        def _on_brain_status(self, _msg: String) -> None:
            self.brain_status_seen = True

        def _refresh_lifecycle_states(self) -> None:
            for name, future in list(self._pending_lifecycle.items()):
                if not future.done():
                    continue
                try:
                    response = future.result()
                    self.lifecycle_states[name] = int(response.current_state.id)
                except Exception:  # noqa: BLE001 - retry an unavailable lifecycle service
                    self.lifecycle_states.pop(name, None)
                self._pending_lifecycle.pop(name, None)

            for name, client in self._lifecycle_clients.items():
                if name not in self._pending_lifecycle and client.service_is_ready():
                    self._pending_lifecycle[name] = client.call_async(GetState.Request())

        def _prune_seen_sensor_subscriptions(self) -> None:
            # Raw camera messages are large.  Once each first sample proves
            # the pipeline is live, stop deserializing frames while other
            # startup checks (notably TensorRT compilation) finish.
            for topic in self.sensor_topics_seen & self._sensor_subscriptions.keys():
                self.destroy_subscription(self._sensor_subscriptions.pop(topic))

        def readiness_errors(self) -> list[str]:
            self._prune_seen_sensor_subscriptions()
            self._refresh_lifecycle_states()
            errors = lifecycle_readiness_errors(self.nav_mode, self.lifecycle_states)

            nodes = {
                _fully_qualified_node_name(name, namespace) for name, namespace in self.get_node_names_and_namespaces()
            }
            required_nodes = REQUIRED_NODES
            required_services = REQUIRED_SERVICES
            if args.allow_missing_cloud:
                # The training node deliberately refuses to start without a
                # service key.  A keyless robot can still become fully usable
                # for its local controls, sensors, navigation, and webapp.
                required_nodes = required_nodes - {"/innate_training"}
                required_services = required_services - {"/innate_training/get_training_status"}

            missing_nodes = sorted(required_nodes - nodes)
            if missing_nodes:
                errors.append("nodes missing: " + ", ".join(missing_nodes))

            services = {name for name, _types in self.get_service_names_and_types()}
            missing_services = sorted(required_services - services)
            if missing_services:
                errors.append("services missing: " + ", ".join(missing_services))

            expected_lifecycle_services = {
                f"{name}/get_state" for name in LIFECYCLE_EXPECTATIONS.get(self.nav_mode, {})
            }
            missing_lifecycle_services = sorted(expected_lifecycle_services - services)
            if missing_lifecycle_services:
                errors.append("lifecycle services missing: " + ", ".join(missing_lifecycle_services))

            missing_actions = sorted(
                name for name, client in self._action_clients.items() if not client.server_is_ready()
            )
            if missing_actions:
                errors.append("actions missing: " + ", ".join(missing_actions))

            if not self.brain_status_seen:
                errors.append("brain/agents not initialized")

            missing_topics = sorted(set(SENSOR_TOPICS) - self.sensor_topics_seen)
            if missing_topics:
                errors.append("sensor data missing: " + ", ".join(missing_topics))

            if not _webapp_ready(args.webapp_url):
                errors.append("webapp not responding")
            if not _tmux_panes_ready(args.tmux_session, args.expected_panes):
                errors.append("launch panes not all running")
            if _tensor_rt_sweep_running():
                errors.append("TensorRT startup sweep still running")
            return errors

    rclpy.init(args=[])
    probe = StartupProbe()

    def advance(duration: float) -> None:
        end = time.monotonic() + duration
        while rclpy.ok() and time.monotonic() < end:
            rclpy.spin_once(probe, timeout_sec=min(0.1, max(0.0, end - time.monotonic())))

    try:
        run_startup_sequence(
            probe.readiness_errors,
            advance,
            lambda: _play_sound(Path(args.sound), args.player),
            timeout_s=args.timeout,
            stable_s=args.stable_for,
            poll_s=args.poll_interval,
            cancelled=lambda: not rclpy.ok(),
        )
    finally:
        probe.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    # A missing dependency or failed sound must be visible in the journal, but
    # must not make systemd restart the otherwise healthy ROS stack.
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sound", help="Audio file to play once startup is ready")
    parser.add_argument("--player", default="gst-play-1.0")
    parser.add_argument("--tmux-session", default="ros_nodes")
    parser.add_argument("--expected-panes", type=int, required=True)
    parser.add_argument("--webapp-url", default="http://127.0.0.1:4080/config.json")
    parser.add_argument("--allow-missing-cloud", action="store_true")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--stable-for", type=float, default=5.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        return _runtime(args)
    except KeyboardInterrupt:
        print("Startup readiness: interrupted before playback")
        return 0
    except Exception as exc:  # noqa: BLE001 - fail closed: never play without proof of readiness
        print(f"Startup readiness: helper failed; sound skipped ({exc})")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

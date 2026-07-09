#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Light-sleep power manager.

Owns the robot's AWAKE <-> SLEEPING state. Sleep powers down the big
consumers (lidar motor, cameras, nav2 stack, arm/head torque, Jetson clocks)
while keeping the network path (zenoh, rosbridge) and this node alive, so the
robot stays remotely wakeable in seconds.

Triggers:
  - /power/sleep, /power/wake (std_srvs/Trigger) — apps, skills, CLI
  - auto-sleep after `sleep_after_idle_minutes` of inactivity (0 = never);
    active SSH connections count as activity
  - wake on joystick input, chat message, or a head boop (head torque is off
    during sleep, but mars_arm keeps publishing the head position, so pushing
    the head is detectable)

State is broadcast on /power/state ("awake" / "going_to_sleep" / "sleeping" /
"waking"), latched + 1 Hz heartbeat, mirroring /brain/agent_status delivery.
"""

import json
import subprocess
import threading
import time

import rclpy
from brain_messages.srv import ChangeNavigationMode
from mars_bringup.config_loader import innate_os_root
from geometry_msgs.msg import Vector3
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import Int32, String
from std_srvs.srv import Empty, SetBool, Trigger

STATE_AWAKE = "awake"
STATE_GOING_TO_SLEEP = "going_to_sleep"
STATE_SLEEPING = "sleeping"
STATE_WAKING = "waking"

# tmux layout from scripts/launch_ros_in_tmux.sh. These panes run the camera
# container and heavy nodes with no role in sleeping or waking; their shells
# keep the sourced ROS env, so we can Ctrl-C the launch to sleep and re-send
# the command to wake. Deliberately kept alive: app-bringup (rosbridge + this
# node + PCB/battery i2c), the arm node (head-boop source), mode_manager (nav
# restore service), input_manager, and console/webapp (the app surface).
TMUX_SESSION = "ros_nodes"
# (window, fallback pane index, process match, relaunch command)
_SLEEP_PANES = [
    ("cam-leader", 0, "camera_composable", "ros2 launch mars_cam camera_composable.launch.py"),
    ("brain-nav", 0, "brain_client.launch.py", "ros2 launch brain_client brain_client.launch.py"),
    ("behaviors-inputs", 0, "behavior.launch.py", "ros2 launch manipulation behavior.launch.py"),
    ("arm-recorder", 1, "recorder.launch.py", "ros2 launch manipulation recorder.launch.py"),
    ("ik-logger", 0, "ik.launch.py", "ros2 launch mars_arm ik.launch.py"),
]

# Terminal statuses from skills_server._publish_skill_status; anything else
# (i.e. "running") means a skill is in flight.
_SKILL_TERMINAL_STATUSES = {"completed", "interrupted", "failed"}

_SS_ESTABLISHED_SSH = ["ss", "-tnH", "state", "established", "( sport = :22 )"]

# Offlines CPU cores 4-5 and caps clocks during sleep — the part of the 7W
# profile nvpmodel refuses to apply without a reboot (see the script header).
_SLEEP_CLOCKS_SCRIPT = str(innate_os_root() / "scripts" / "power" / "sleep-clocks.sh")


class PowerManager(Node):
    def __init__(self):
        super().__init__("power_manager")

        self.declare_parameter("sleep_after_idle_minutes", 0)
        self.declare_parameter("boop_wake_threshold_degrees", 4)

        self._state = STATE_AWAKE
        self._state_lock = threading.Lock()
        self._transition_lock = threading.Lock()

        self._last_activity = time.monotonic()
        self._skill_in_flight = False
        self._ssh_active = False  # cached; refreshed off-executor by _idle_tick
        self._ssh_check_inflight = False
        self._nav_mode = "none"  # live value from /nav/current_mode
        self._nav_mode_before_sleep = "none"
        self._brain_active = False  # live value from /brain/agent_status
        self._brain_active_before_sleep = False
        self._pane_ids: dict[str, str] = {}  # process match -> tmux pane id, saved at stop

        # Head-boop detection (armed while sleeping)
        self._head_baseline_deg: float | None = None
        self._boop_arm_time = 0.0
        self._boop_hits = 0
        self._last_boop_sample = 0.0

        latched = QoSProfile(
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            reliability=QoSReliabilityPolicy.RELIABLE,
        )
        self._state_pub = self.create_publisher(String, "/power/state", latched)
        self._head_position_pub = self.create_publisher(Int32, "/mars/head/set_position", 10)

        self.create_service(Trigger, "/power/sleep", self._on_sleep_request)
        self.create_service(Trigger, "/power/wake", self._on_wake_request)

        # Downstream controls
        self._brain_active_client = self.create_client(SetBool, "/brain/set_brain_active")
        self._nav_mode_client = self.create_client(ChangeNavigationMode, "/nav/change_mode")
        self._lidar_stop_client = self.create_client(Empty, "/stop_motor")
        self._lidar_start_client = self.create_client(Empty, "/start_motor")
        self._arm_torque_off_client = self.create_client(Trigger, "/mars/arm/torque_off")
        self._arm_torque_on_client = self.create_client(Trigger, "/mars/arm/torque_on")
        self._head_servo_client = self.create_client(SetBool, "/mars/head/enable_servo")
        self._arm_low_power_client = self.create_client(SetBool, "/mars/arm/low_power_mode")

        # Activity signals
        self.create_subscription(Vector3, "/joystick", self._on_joystick, 10)
        self.create_subscription(String, "/brain/chat_in", self._on_chat_in, 10)
        self.create_subscription(String, "/webrtc/active_streams", self._on_webrtc_streams, 10)
        self.create_subscription(String, "/brain/skill_status_update", self._on_skill_status, 10)
        self.create_subscription(String, "/nav/current_mode", self._on_nav_mode, 10)
        self.create_subscription(String, "/brain/agent_status", self._on_agent_status, latched)
        self.create_subscription(String, "/mars/head/current_position", self._on_head_position, 10)

        self.create_timer(1.0, self._publish_state)
        self.create_timer(10.0, self._idle_tick)

        self.get_logger().info("Power manager ready (state: awake)")

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self._state = state
        self._state_pub.publish(String(data=state))
        self.get_logger().info(f"Power state: {state}")

    def _publish_state(self) -> None:
        self._state_pub.publish(String(data=self.state))

    # -------------------------------------------------------------- services

    def _on_sleep_request(self, request, response):
        del request
        started = self._start_transition(
            self._sleep_sequence, from_state=STATE_AWAKE, during_state=STATE_GOING_TO_SLEEP
        )
        response.success = started
        response.message = "Going to sleep" if started else f"Cannot sleep from state '{self.state}'"
        return response

    def _on_wake_request(self, request, response):
        del request
        started = self._start_transition(
            self._wake_sequence, from_state=STATE_SLEEPING, during_state=STATE_WAKING, reason="wake service"
        )
        response.success = started
        response.message = "Waking up" if started else f"Cannot wake from state '{self.state}'"
        return response

    def _start_transition(self, sequence, from_state: str, during_state: str, reason: str = "") -> bool:
        """Kick off a sleep/wake sequence in a worker thread (service callbacks
        must not block the executor — the sequences themselves call services).

        `during_state` is passed explicitly rather than derived from `sequence`:
        bound methods are recreated per attribute access, so identity checks
        like `sequence is self._sleep_sequence` are always False."""
        with self._state_lock:
            if self._state != from_state:
                return False
            self._state = during_state
        self._state_pub.publish(String(data=self.state))
        self.get_logger().info(f"Power state: {self.state}" + (f" ({reason})" if reason else ""))
        threading.Thread(target=self._run_transition, args=(sequence, during_state), daemon=True).start()
        return True

    def _run_transition(self, sequence, during_state: str) -> None:
        with self._transition_lock:
            try:
                sequence()
            except Exception as e:  # a half-finished transition is recoverable via the other service
                self.get_logger().error(f"Power transition failed: {e}")
                self._set_state(STATE_SLEEPING if during_state == STATE_GOING_TO_SLEEP else STATE_AWAKE)

    # ------------------------------------------------------------- activity

    def _touch_activity(self) -> None:
        self._last_activity = time.monotonic()

    def _on_joystick(self, msg: Vector3) -> None:
        # driveController publishes a zero vector on release/pagehide — only
        # real input counts as activity (and wakes the robot).
        if abs(msg.x) < 1e-3 and abs(msg.y) < 1e-3 and abs(msg.z) < 1e-3:
            return
        self._touch_activity()
        self._wake_if_sleeping("joystick input")

    def _on_chat_in(self, msg: String) -> None:
        del msg
        self._touch_activity()
        self._wake_if_sleeping("chat message")

    def _on_webrtc_streams(self, msg: String) -> None:
        try:
            count = json.loads(msg.data).get("count", 0)
        except (json.JSONDecodeError, AttributeError):
            return
        if count > 0:
            self._touch_activity()

    def _on_skill_status(self, msg: String) -> None:
        self._touch_activity()
        try:
            status = json.loads(msg.data).get("status", "")
        except json.JSONDecodeError:
            return
        self._skill_in_flight = status not in _SKILL_TERMINAL_STATUSES

    def _on_nav_mode(self, msg: String) -> None:
        self._nav_mode = msg.data

    def _on_agent_status(self, msg: String) -> None:
        try:
            self._brain_active = bool(json.loads(msg.data).get("brain_active", False))
        except json.JSONDecodeError:
            pass

    def _refresh_ssh_activity(self) -> None:
        """Runs on a short-lived thread — `ss` can stall for seconds under load,
        and _idle_tick is an executor callback that must never block."""
        try:
            out = subprocess.run(_SS_ESTABLISHED_SSH, capture_output=True, text=True, timeout=5)
            self._ssh_active = bool(out.stdout.strip())
        except (OSError, subprocess.SubprocessError) as e:
            self.get_logger().warning(f"SSH activity check failed: {e}")
            self._ssh_active = False
        finally:
            self._ssh_check_inflight = False

    def _idle_tick(self) -> None:
        if self.state != STATE_AWAKE:
            return
        idle_minutes = self.get_parameter("sleep_after_idle_minutes").value
        if idle_minutes <= 0:
            return
        if self._skill_in_flight or self._nav_mode in ("mapping", "switching"):
            self._touch_activity()
            return
        if not self._ssh_check_inflight:
            self._ssh_check_inflight = True
            threading.Thread(target=self._refresh_ssh_activity, daemon=True).start()
        # Reads the previous tick's result — one 10 s tick of lag is nothing
        # against an idle window measured in minutes.
        if self._ssh_active:
            self._touch_activity()
            return
        if time.monotonic() - self._last_activity >= idle_minutes * 60:
            self.get_logger().info(f"Idle for {idle_minutes} minutes — going to sleep")
            self._start_transition(
                self._sleep_sequence,
                from_state=STATE_AWAKE,
                during_state=STATE_GOING_TO_SLEEP,
                reason="idle timeout",
            )

    # ------------------------------------------------------------- head boop

    def _on_head_position(self, msg: String) -> None:
        if self.state != STATE_SLEEPING or time.monotonic() < self._boop_arm_time:
            return
        # The arm node streams this at its control-loop rate; sample at 10 Hz.
        if time.monotonic() - self._last_boop_sample < 0.1:
            return
        self._last_boop_sample = time.monotonic()
        try:
            position = float(json.loads(msg.data).get("current_position"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if self._head_baseline_deg is None:
            self._head_baseline_deg = position
            return
        threshold = self.get_parameter("boop_wake_threshold_degrees").value
        delta = position - self._head_baseline_deg
        if abs(delta) >= threshold:
            self._boop_hits += 1
            # two consecutive readings past the threshold, so a single glitched
            # sample or a floor vibration doesn't wake the robot
            if self._boop_hits >= 2:
                self._wake_if_sleeping("head boop")
        else:
            self._boop_hits = 0
            # Creep-track the baseline (≤0.5°/s): a torque-less head can sag
            # slowly under gravity, and untracked sag would eventually read as
            # a boop. A real boop is a fast >threshold push the creep can't absorb.
            self._head_baseline_deg += max(-0.05, min(0.05, delta))

    def _wake_if_sleeping(self, reason: str) -> None:
        if self.state == STATE_SLEEPING:
            self._start_transition(
                self._wake_sequence, from_state=STATE_SLEEPING, during_state=STATE_WAKING, reason=reason
            )

    # ------------------------------------------------------------- sequences

    def _sleep_sequence(self) -> None:
        # Let whatever triggered us (e.g. the go_to_sleep skill) finish its
        # goodbye before we start tearing subsystems down.
        time.sleep(2.0)

        self._nav_mode_before_sleep = self._nav_mode
        self._brain_active_before_sleep = self._brain_active

        self._droop_head()
        self._call(self._brain_active_client, SetBool.Request(data=False), "brain off")
        self._call(
            self._nav_mode_client,
            ChangeNavigationMode.Request(mode="off"),
            "nav stack off",
            timeout=45.0,
        )
        self._call(self._lidar_stop_client, Empty.Request(), "lidar motor off")
        self._call(self._arm_torque_off_client, Trigger.Request(), "arm torque off")
        self._call(self._head_servo_client, SetBool.Request(data=False), "head torque off")
        # ~10 Hz is plenty for the boop detector, and the 200 Hz stream is the
        # main CPU load during sleep (it fans out through /joint_states -> /tf
        # into every Python subscriber and TF listener).
        self._call(self._arm_low_power_client, SetBool.Request(data=True), "arm loop 10Hz")
        self._stop_sleep_panes()
        self._run_privileged(["systemctl", "stop", "speaker-keepalive.service"])
        self._run_privileged(["systemctl", "stop", "jetson-perf.service"])
        # 15W profile — the lowest reachable live: 7W (-m 3) takes CPU cores
        # offline, which nvpmodel only does across a reboot (it prompts and
        # aborts, rc=234 "Golden image context is already created").
        self._run_privileged(["/usr/sbin/nvpmodel", "-m", "0"])
        # ...then do the core-offline + clock-cap part ourselves via sysfs
        # (must run after nvpmodel, which rewrites the cpufreq limits).
        self._run_privileged([_SLEEP_CLOCKS_SCRIPT, "sleep"])

        # Arm the boop detector: give the torque-less head a moment to settle,
        # then take the first reading after that as the baseline.
        self._head_baseline_deg = None
        self._boop_hits = 0
        self._boop_arm_time = time.monotonic() + 2.0

        self._set_state(STATE_SLEEPING)

    def _wake_sequence(self) -> None:
        # Compute first so the robot feels responsive from the very first second.
        # jetson-perf pins nvpmodel -m 2 + jetson_clocks in its loop; the explicit
        # nvpmodel call restores the power budget even if the service is absent.
        self._run_privileged([_SLEEP_CLOCKS_SCRIPT, "wake"])
        self._run_privileged(["/usr/sbin/nvpmodel", "-m", "2"])
        self._run_privileged(["systemctl", "start", "jetson-perf.service"])
        # Relaunch the stopped panes first — brain/MoveIt startup overlaps the
        # rest of the wake sequence.
        self._start_sleep_panes()
        self._call(self._lidar_start_client, Empty.Request(), "lidar motor on")
        self._call(self._arm_low_power_client, SetBool.Request(data=False), "arm loop full rate")
        self._call(self._head_servo_client, SetBool.Request(data=True), "head torque on")
        self._call(self._arm_torque_on_client, Trigger.Request(), "arm torque on")
        self._run_privileged(["systemctl", "start", "speaker-keepalive.service"])

        if self._nav_mode_before_sleep in ("navigation", "mapping", "mapfree"):
            self._call(
                self._nav_mode_client,
                ChangeNavigationMode.Request(mode=self._nav_mode_before_sleep),
                f"nav stack -> {self._nav_mode_before_sleep}",
                timeout=60.0,
            )
        if self._brain_active_before_sleep:
            # brain_client is still booting from the pane relaunch above.
            self._call(self._brain_active_client, SetBool.Request(data=True), "brain on", wait=30.0)

        self._head_position_pub.publish(Int32(data=0))
        self._touch_activity()
        self._set_state(STATE_AWAKE)

    def _droop_head(self) -> None:
        """Slow droop to the head's lower limit — the visible 'falling asleep' cue."""
        for angle in (-5, -12, -20, -25):
            self._head_position_pub.publish(Int32(data=angle))
            time.sleep(0.35)

    # -------------------------------------------------------------- helpers

    def _call(self, client, request, label: str, timeout: float = 10.0, wait: float = 3.0) -> None:
        """Fire a service call from the transition thread and wait for it.

        Failures are logged and skipped: a missing subsystem (e.g. lidar
        unplugged on a dev bench) must not abort the whole transition."""
        if not client.wait_for_service(timeout_sec=wait):
            self.get_logger().warning(f"[{label}] service {client.srv_name} unavailable, skipping")
            return
        future = client.call_async(request)
        deadline = time.monotonic() + timeout
        while not future.done():
            if time.monotonic() > deadline:
                self.get_logger().warning(f"[{label}] timed out after {timeout}s")
                return
            time.sleep(0.05)
        self.get_logger().info(f"[{label}] done")

    def _run_privileged(self, cmd: list[str]) -> None:
        """Run a whitelisted root command (sudoers entries installed by
        scripts/update/post_update.sh). Degrades to a warning if not granted."""
        try:
            result = subprocess.run(
                ["sudo", "-n", *cmd],
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                self.get_logger().warning(
                    f"'{' '.join(cmd)}' failed (rc={result.returncode}): "
                    f"{result.stderr.strip() or result.stdout.strip()} — "
                    "run scripts/update/post_update.sh to install the sudoers entries"
                )
        except (OSError, subprocess.SubprocessError) as e:
            self.get_logger().warning(f"'{' '.join(cmd)}' failed: {e}")

    def _tmux(self, *args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            self.get_logger().warning(f"tmux {' '.join(args)} failed: {e}")
            return None

    def _find_pane(self, window: str, match: str) -> str | None:
        """The pane in `window` whose shell has `match` in a child process's args."""
        listing = self._tmux(
            "list-panes", "-t", f"{TMUX_SESSION}:{window}", "-F", "#{pane_id} #{pane_pid}"
        )
        if listing is None or listing.returncode != 0:
            return None
        for line in listing.stdout.splitlines():
            pane_id, _, pane_pid = line.partition(" ")
            try:
                children = subprocess.run(
                    ["ps", "--ppid", pane_pid.strip(), "-o", "args="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            if match in children:
                return pane_id
        return None

    def _stop_sleep_panes(self) -> None:
        stopped = []
        for window, _, match, _ in _SLEEP_PANES:
            pane = self._find_pane(window, match)
            if pane is None:
                self.get_logger().warning(f"[{match}] pane not found — may already be down")
                continue
            self._pane_ids[match] = pane
            self._tmux("send-keys", "-t", pane, "C-c")
            stopped.append(match)
        time.sleep(5.0)  # ros2 launch tears everything down gracefully on SIGINT
        self.get_logger().info(f"[panes off] {', '.join(stopped) if stopped else 'none found'}")

    def _start_sleep_panes(self) -> None:
        for window, fallback_index, match, command in _SLEEP_PANES:
            if self._find_pane(window, match) is not None:
                self.get_logger().info(f"[{match}] already running")
                continue
            # Fall back to the launcher's fixed pane layout if we never saw
            # the pane (e.g. woke after a power_manager restart).
            pane = self._pane_ids.get(match) or f"{TMUX_SESSION}:{window}.{fallback_index}"
            self._tmux("send-keys", "-t", pane, command, "Enter")
        self.get_logger().info("[panes on] launches sent")


def main(args=None):
    rclpy.init(args=args)
    node = PowerManager()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()

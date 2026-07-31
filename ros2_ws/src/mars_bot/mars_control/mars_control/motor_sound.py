#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Plays an electric motor whine through the robot's speaker, driven by speed.

The synthesis itself lives in ``motor_synth`` and knows nothing about ROS.
This node's only jobs are to work out how hard the motor is working
and to keep the audio device fed:

* road speed and throttle both come from ``/cmd_vel``. Commanded speed *is*
  road speed on this base -- the wheel controller tracks its setpoint, and
  the command arrives already smoothed by the teleop shaper -- whereas
  ``/odom`` here carries pose only, its twist never filled in, which made
  the whine deaf to speed when it listened there.
* throttle demand is the commanded acceleration, which is what makes
  accelerating sound different from cruising at the same speed.

Audio goes to the ALSA ``default`` device, which ``config/alsa/asound.conf``
defines as dmix + softvol. dmix is what lets this run alongside TTS instead of
fighting it for the device.
"""

import json
import time

import rclpy
from geometry_msgs.msg import Twist
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from std_msgs.msg import String

from mars_control.motor_synth import MotorConfig, MotorSynth, is_mad_scale

CMD_TIMEOUT_S = 0.5
"""How long a /cmd_vel stays fresh. Matches the base's own command watchdog,
so when the sound decides the robot has stopped, the motors agree."""
STOPPED_SPEED = 0.02
"""Below this, in m/s, the robot counts as parked."""
IDLE_THROB = 0.3
"""Load a parked-but-idling motor carries, so the resting sound rumbles like
a lopey idle instead of humming sterile. Only heard when idle_when_stopped
keeps the synth running while parked."""


def _clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


class MotorSoundNode(Node):
    def __init__(self):
        super().__init__("motor_sound")

        self._declare_parameters()
        params = self.get_parameters_by_prefix("motor_sound")
        self._enabled = params["enabled"].value
        self._idle_when_stopped = params["idle_when_stopped"].value
        self._only_in_mad_mode = params["only_in_mad_mode"].value
        self._mad_active = False
        self._max_speed = max(float(params["max_speed"].value), 1e-3)
        self._reference_acceleration = max(float(params["reference_acceleration"].value), 1e-3)

        self._synth = MotorSynth(int(params["sample_rate"].value), self._build_config(params))
        self._synth.volume = float(params["volume"].value)

        # Written by the ROS executor thread, read by the audio thread. Both
        # are plain floats, so the GIL makes each access atomic and no lock
        # is needed -- which matters, because blocking the audio thread on a
        # lock would produce dropouts.
        self._commanded_speed = 0.0
        self._acceleration = 0.0
        self._commanded_at = 0.0

        self.create_subscription(Twist, "/cmd_vel", self._on_cmd_vel, 10)
        # Speed mode is robot state, republished by mars_app at 1 Hz. Gating on
        # it here (rather than having a client toggle a parameter) means every
        # way of entering Mad -- webapp, mobile app, hand-written write -- gets
        # the engine, and leaving it always silences.
        self.create_subscription(String, "/robot/info", self._on_robot_info, 10)
        self.add_on_set_parameters_callback(self._on_parameter_change)
        # /cmd_vel falls silent once nothing drives the base, so a slow tick
        # is what winds the sound down after the last command goes stale.
        self.create_timer(0.1, self._update_drive)

        self._stream = self._open_stream(params)
        if self._stream is not None and not self._only_in_mad_mode:
            self._synth.trigger_startup()
        self._update_drive()

    def _declare_parameters(self):
        self.declare_parameters(
            namespace="",
            parameters=[
                ("motor_sound.enabled", True),
                ("motor_sound.volume", 0.5),
                ("motor_sound.idle_when_stopped", True),
                ("motor_sound.only_in_mad_mode", True),
                ("motor_sound.max_speed", 0.4),
                ("motor_sound.reference_acceleration", 0.6),
                ("motor_sound.base_hz", 180.0),
                ("motor_sound.top_hz", 950.0),
                ("motor_sound.gear_tops", [0.5, 1.0]),
                ("motor_sound.shift_seconds", 0.12),
                ("motor_sound.spring_hz", 1.2),
                ("motor_sound.damping", 0.55),
                ("motor_sound.wobble_hz", 5.5),
                ("motor_sound.wobble_depth", 0.018),
                ("motor_sound.brush_depth", 0.35),
                ("motor_sound.growl_hz", 26.0),
                ("motor_sound.growl_depth", 0.5),
                ("motor_sound.growl_noise_level", 0.20),
                ("motor_sound.rolling_level", 0.14),
                ("motor_sound.startup_seconds", 0.9),
                ("motor_sound.idle_level", 0.12),
                ("motor_sound.sample_rate", 48000),
                ("motor_sound.blocksize", 512),
                ("motor_sound.device", ""),
            ],
        )

    def _build_config(self, params) -> MotorConfig:
        return MotorConfig(
            base_hz=float(params["base_hz"].value),
            top_hz=float(params["top_hz"].value),
            max_speed=self._max_speed,
            gear_tops=tuple(float(top) for top in params["gear_tops"].value),
            shift_seconds=float(params["shift_seconds"].value),
            spring_hz=float(params["spring_hz"].value),
            damping=float(params["damping"].value),
            wobble_hz=float(params["wobble_hz"].value),
            wobble_depth=float(params["wobble_depth"].value),
            brush_depth=float(params["brush_depth"].value),
            growl_hz=float(params["growl_hz"].value),
            growl_depth=float(params["growl_depth"].value),
            growl_noise_level=float(params["growl_noise_level"].value),
            rolling_level=float(params["rolling_level"].value),
            startup_seconds=float(params["startup_seconds"].value),
            idle_level=float(params["idle_level"].value),
            volume=float(params["volume"].value),
        )

    def _open_stream(self, params):
        """Open the output stream, or carry on silently if there is no audio
        device -- the simulator container has none, and the node should not
        take the rest of the stack down with it."""
        try:
            import sounddevice
        except (ImportError, OSError) as exc:
            # OSError: the PortAudio shared library is missing.
            self.get_logger().warning(f"motor sound disabled, no audio backend: {exc}")
            return None

        device = params["device"].value or None
        try:
            stream = sounddevice.OutputStream(
                samplerate=int(params["sample_rate"].value),
                blocksize=int(params["blocksize"].value),
                channels=2,
                dtype="float32",
                device=device,
                callback=self._audio_callback,
            )
            stream.start()
        except Exception as exc:
            self.get_logger().warning(f"motor sound disabled, could not open audio device: {exc}")
            return None

        self.get_logger().info(f"motor sound running at {stream.samplerate:.0f} Hz, block {stream.blocksize}")
        return stream

    def _audio_callback(self, outdata, frames, _time_info, _status):
        """Runs on PortAudio's thread. Must never block or log -- an exception
        escaping here silently aborts the stream, so it swallows everything and
        outputs silence instead."""
        try:
            mono = self._synth.render(frames)
            outdata[:, 0] = mono
            outdata[:, 1] = mono
        except Exception:
            outdata.fill(0.0)

    def _on_robot_info(self, msg: String):
        try:
            mad = is_mad_scale(json.loads(msg.data))
        except (ValueError, TypeError):
            mad = False
        if mad and not self._mad_active:
            # Entering Mad announces itself: the spin-up chirp doubles as the
            # mode-change cue.
            self._synth.trigger_startup()
        self._mad_active = mad
        self._update_drive()

    def _on_cmd_vel(self, msg: Twist):
        now = time.monotonic()
        elapsed = now - self._commanded_at
        if 0.0 < elapsed < CMD_TIMEOUT_S:
            measured = (abs(msg.linear.x) - abs(self._commanded_speed)) / elapsed
            # Keyboard teleop and the mux's stop are steps; smoothing turns
            # them into a swell the growl can ride instead of a one-tick spike.
            self._acceleration += (measured - self._acceleration) * 0.3
        else:
            self._acceleration = 0.0
        self._commanded_speed = msg.linear.x
        self._commanded_at = now
        self._update_drive()

    def _update_drive(self):
        """Translate the commanded motion into speed + throttle for the synth."""
        if time.monotonic() - self._commanded_at > CMD_TIMEOUT_S:
            # Nothing is driving the base, and its own watchdog has zeroed the
            # motors by now -- the sound winds down with them.
            self._commanded_speed = 0.0
            self._acceleration = 0.0
        speed = abs(self._commanded_speed)

        # Holding a steady speed still takes some throttle, so the motor is
        # never completely off-load while moving.
        throttle = 0.35 * min(speed / self._max_speed, 1.0)
        # Speeding up is "foot down"; slowing down is not (negatives clamp to 0).
        throttle = max(throttle, _clamp01(self._acceleration / self._reference_acceleration))

        parked = speed < STOPPED_SPEED
        if parked:
            throttle = max(throttle, IDLE_THROB)
        allowed = bool(self._enabled) and (self._mad_active or not self._only_in_mad_mode)
        self._synth.enabled = allowed and (self._idle_when_stopped or not parked)
        self._synth.set_drive(speed, throttle)

    def _on_parameter_change(self, params):
        for param in params:
            if param.name == "motor_sound.enabled":
                was_enabled = self._enabled
                self._enabled = bool(param.value)
                if self._enabled and not was_enabled:
                    self._synth.trigger_startup()
            elif param.name == "motor_sound.volume":
                self._synth.volume = float(param.value)
            elif param.name == "motor_sound.idle_when_stopped":
                self._idle_when_stopped = bool(param.value)
            elif param.name == "motor_sound.only_in_mad_mode":
                self._only_in_mad_mode = bool(param.value)
        self._update_drive()
        return SetParametersResult(successful=True)

    def destroy_node(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = MotorSoundNode()
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

#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Subscribe to /audio/remote_mic and play it through aplay.

The publisher (mars_cam webrtc) emits innate_audio/Audio: S16LE, 48000 Hz, mono,
best-effort QoS. We forward the raw int16 samples straight into aplay's stdin.

Playback is gated on a std_msgs/Bool topic (/audio/remote_mic/to_speaker):
audio only reaches aplay while the latest value is True. Samples received while
gated off are dropped, so re-enabling plays live audio, not a buffered backlog.

aplay is spawned per gate-on and killed on gate-off (the ALSA default is dmix,
so holding it open would be harmless, but a crashed aplay must not be fatal:
it is respawned on the next chunk instead of taking the node down).
"""

import subprocess
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import Bool

from innate_audio.msg import Audio

RATE = 48000
CHANNELS = 1
GATE_TOPIC = "/audio/remote_mic/to_speaker"
# Gate on but no audio for this long -> the inbound mic path is down, warn.
NO_AUDIO_WARN_S = 5.0


class RemoteMicPlayer(Node):
    def __init__(self):
        super().__init__("remote_mic_player")
        self.play_enabled = False
        self.aplay = None
        self.last_audio_t = None
        self.enabled_t = None
        self.no_audio_warned = False
        self.gated_drop_count = 0
        self.last_gated_log_t = 0.0
        self.chunks_played = 0
        self.sub = self.create_subscription(
            Audio, "/audio/remote_mic", self.on_audio, qos_profile_sensor_data
        )
        self.gate_sub = self.create_subscription(Bool, GATE_TOPIC, self.on_gate, 10)
        self.watchdog = self.create_timer(1.0, self.check_flow)
        self.get_logger().info(f"Ready: /audio/remote_mic -> aplay, gated on {GATE_TOPIC} (off until True)")

    def _spawn_aplay(self):
        self.aplay = subprocess.Popen(
            ["aplay", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", str(CHANNELS), "-q"],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.get_logger().info(f"aplay started (pid {self.aplay.pid})")

    def _stop_aplay(self, reason: str):
        if self.aplay is None:
            return
        proc, self.aplay = self.aplay, None
        if proc.poll() is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            self.get_logger().info(f"aplay stopped ({reason})")
        else:
            stderr = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
            self.get_logger().error(
                f"aplay had died (rc={proc.returncode}{', ' + stderr if stderr else ''}); {reason}"
            )

    def on_gate(self, msg: Bool):
        if msg.data == self.play_enabled:
            return
        self.play_enabled = msg.data
        self.get_logger().info(f"Playback {'enabled' if msg.data else 'disabled'} (gate {GATE_TOPIC})")
        if msg.data:
            self.enabled_t = time.monotonic()
            self.no_audio_warned = False
            self.chunks_played = 0
            self.gated_drop_count = 0
        else:
            self._stop_aplay("gate off")

    def on_audio(self, msg: Audio):
        now = time.monotonic()
        self.last_audio_t = now
        if not self.play_enabled:
            # Audio IS arriving but the speaker gate is off — the discriminator between
            # "mic path down" and "gate Bool never reached us". Logged sparsely.
            self.gated_drop_count += 1
            if now - self.last_gated_log_t > 10.0:
                self.last_gated_log_t = now
                self.get_logger().info(
                    f"Receiving /audio/remote_mic (seq {msg.seq}) but gate is off — "
                    f"dropped {self.gated_drop_count} chunks while gated off"
                )
            return
        if self.aplay is not None and self.aplay.poll() is not None:
            self._stop_aplay("respawning")
        if self.aplay is None:
            self._spawn_aplay()
        try:
            # int16[] samples -> little-endian raw bytes for aplay's stdin.
            self.aplay.stdin.write(bytes(memoryview(msg.samples).cast("B")))
            self.aplay.stdin.flush()
        except (BrokenPipeError, OSError):
            self._stop_aplay("write failed")
            return
        self.chunks_played += 1
        if self.chunks_played == 1:
            self.get_logger().info(f"Playing (first chunk: seq {msg.seq}, rate {msg.rate}, {len(msg.samples)} samples)")

    def check_flow(self):
        if not self.play_enabled:
            return
        now = time.monotonic()
        since = now - max(self.last_audio_t or 0.0, self.enabled_t or 0.0)
        if since > NO_AUDIO_WARN_S:
            if not self.no_audio_warned:
                self.no_audio_warned = True
                self.get_logger().warning(
                    f"Gate is ON but no /audio/remote_mic data for {since:.0f}s — "
                    "is the operator mic toggle on and the WebRTC peer connected?"
                )
        elif self.no_audio_warned:
            self.no_audio_warned = False
            self.get_logger().info("Audio flow resumed")

    def destroy_node(self):
        self._stop_aplay("shutdown")
        super().destroy_node()


def main():
    rclpy.init()
    node = RemoteMicPlayer()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main() or 0)

#!/usr/bin/env python3
"""Subscribe to /audio/remote_mic and play it through aplay.

The publisher (mars_cam webrtc) emits innate_audio/Audio: S16LE, 48000 Hz, mono,
best-effort QoS. We forward the raw int16 samples straight into aplay's stdin.

Playback is gated on a std_msgs/Bool topic (/audio/remote_mic/to_speaker):
audio only reaches aplay while the latest value is True. Samples received while
gated off are dropped, so re-enabling plays live audio, not a buffered backlog.
"""

import subprocess
import sys

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from std_msgs.msg import Bool

from innate_audio.msg import Audio

RATE = 48000
CHANNELS = 1
GATE_TOPIC = "/audio/remote_mic/to_speaker"


class RemoteMicPlayer(Node):
    def __init__(self):
        super().__init__("remote_mic_player")
        self.play_enabled = False
        self.aplay = subprocess.Popen(
            ["aplay", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", str(CHANNELS), "-q"],
            stdin=subprocess.PIPE,
        )
        self.sub = self.create_subscription(
            Audio, "/audio/remote_mic", self.on_audio, qos_profile_sensor_data
        )
        self.gate_sub = self.create_subscription(Bool, GATE_TOPIC, self.on_gate, 10)
        self.get_logger().info(f"Ready: /audio/remote_mic -> aplay, gated on {GATE_TOPIC} (off until True)")

    def on_gate(self, msg: Bool):
        if msg.data != self.play_enabled:
            self.play_enabled = msg.data
            self.get_logger().info(f"Playback {'enabled' if msg.data else 'disabled'}")

    def on_audio(self, msg: Audio):
        if not self.play_enabled:
            return
        if self.aplay.poll() is not None:
            self.get_logger().error("aplay exited; shutting down")
            rclpy.shutdown()
            return
        # int16[] samples -> little-endian raw bytes for aplay's stdin.
        self.aplay.stdin.write(bytes(memoryview(msg.samples).cast("B")))
        self.aplay.stdin.flush()

    def destroy_node(self):
        if self.aplay.stdin:
            self.aplay.stdin.close()
        self.aplay.wait()
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

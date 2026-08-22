# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import base64
import io
import subprocess
import threading
import wave
from tempfile import NamedTemporaryFile

from rclpy.node import Node
from std_msgs.msg import String

from innate import Skill, SkillReturn, resource

MIC_CAPTURE_TOPIC = "/mic/capture"
TTS_AUDIO_TOPIC = "/tts/audio"
TTS_STATUS_TOPIC = "/tts/is_playing"
SAMPLE_RATE = 24_000
MIN_RECORD_SECONDS = 1.0
MAX_RECORD_SECONDS = 15.0


class PcmRecorder:
    def __init__(self, node: Node):
        self._chunks: list[bytes] = []
        self._recording = False
        self._lock = threading.Lock()
        self._sub = node.create_subscription(String, MIC_CAPTURE_TOPIC, self._on_audio, 10)

    def _on_audio(self, msg: String) -> None:
        try:
            chunk = base64.b64decode(msg.data, validate=True)
        except (ValueError, TypeError):
            return
        if not chunk or len(chunk) % 2:
            return
        with self._lock:
            if self._recording:
                self._chunks.append(chunk)

    def start(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._recording = True

    def stop(self) -> bytes:
        with self._lock:
            self._recording = False
            return b"".join(self._chunks)


class Playback(Skill):
    """Record a short microphone clip and play back exactly what Mars heard."""

    @resource
    def _recorder(self) -> PcmRecorder:
        return PcmRecorder(self.node)

    def guidelines(self) -> str:
        return (
            "Use when the user asks Mars to record them, echo the microphone, or play back "
            "what it hears. The optional record_seconds controls the recording length."
        )

    def execute(self, record_seconds: float = 5.0) -> SkillReturn:
        if not MIN_RECORD_SECONDS <= record_seconds <= MAX_RECORD_SECONDS:
            self.fail(f"record_seconds must be between {MIN_RECORD_SECONDS:g} and {MAX_RECORD_SECONDS:g}")

        self.say("I want you to talk.", wait=True)
        self._recorder.start()
        try:
            self.sleep(record_seconds)
        finally:
            pcm = self._recorder.stop()

        if not pcm:
            self.fail("No microphone audio was captured")

        wav = self._wav(pcm)
        if self._has_speaker():
            self._play_on_robot(wav)
        else:
            self._play_in_browser(wav)
        return f"Recorded and played back {record_seconds:g} seconds of microphone audio"

    @staticmethod
    def _wav(pcm: bytes) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as clip:
            clip.setnchannels(1)
            clip.setsampwidth(2)
            clip.setframerate(SAMPLE_RATE)
            clip.writeframes(pcm)
        return output.getvalue()

    @staticmethod
    def _has_speaker() -> bool:
        try:
            result = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=2.0, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and "card " in result.stdout

    def _play_on_robot(self, wav: bytes) -> None:
        status = self.node.create_publisher(String, TTS_STATUS_TOPIC, 10)
        self.wait_for(lambda: True if status.get_subscription_count() else None, timeout=1.0)
        with NamedTemporaryFile(suffix=".wav") as clip:
            clip.write(wav)
            clip.flush()
            player = subprocess.Popen(
                ["aplay", "-q", clip.name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self.on_cancel(player.terminate)
            status.publish(String(data="true"))
            try:
                while player.poll() is None:
                    self.sleep(0.05)
            finally:
                status.publish(String(data="false"))

        if player.returncode:
            stderr = player.stderr.read().decode(errors="replace").strip() if player.stderr else ""
            self.fail(f"Audio playback failed: {stderr or f'aplay exited with {player.returncode}'}")

    def _play_in_browser(self, wav: bytes) -> None:
        publisher = self.node.create_publisher(String, TTS_AUDIO_TOPIC, 10)
        self.wait_for(lambda: True if publisher.get_subscription_count() else None, timeout=1.0)
        publisher.publish(String(data=base64.b64encode(wav).decode("ascii")))

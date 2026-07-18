#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Microphone Input Device

Transcribes voice via OpenAI's realtime API. Runs two independent streams:
- "local"  — the on-robot hardware mic (arecord), gated by /audio/local_mic/stt (default ON)
- "remote" — the inbound phone mic on /audio/remote_mic, gated by /audio/remote_mic/stt (default OFF)

Each stream owns its own OpenAI connection and transcribes separately. This is a
pure Python class with NO ROS dependencies beyond the node handle the manager
injects (used only to subscribe to the control + remote-audio topics).

Uses proxy services via self.proxy (injected by InputManager).
"""

import array
import audioop
import base64
import json
import queue
import re
import subprocess
import threading
import time

import sounddevice as sd

from brain_client.common.logging import UniversalLogger
from brain_client.inputs.types import InputDevice

DEFAULT_SAMPLE_RATE = 24_000  # rate the OpenAI Realtime API expects (pcm16)
DEFAULT_CHANNELS = 1
DTYPE = "int16"
CHUNK_DURATION_SEC = 0.02

LOCAL_STT_TOPIC = "/audio/local_mic/stt"
REMOTE_STT_TOPIC = "/audio/remote_mic/stt"
REMOTE_MIC_TOPIC = "/audio/remote_mic"


class MicroInput(InputDevice):
    """
    Microphone input device — orchestrates a local and a remote transcription stream.

    Each stream connects to the OpenAI Realtime API via proxy (self.proxy.openai.realtime)
    and emits transcripts independently. The local stream is on by default (preserving the
    old behaviour when /audio/local_mic/stt is never published); the remote stream is off
    until /audio/remote_mic/stt publishes true.

    Supports "ducking" - suppresses audio while robot is speaking.
    Streams automatically reconnect if their WebSocket connection is lost.
    """

    def __init__(self):
        super().__init__()
        self._local: TranscriptionStream | None = None
        self._remote: TranscriptionStream | None = None
        self._local_enabled = True  # old stt on if /audio/local_mic/stt never published
        self._remote_enabled = False  # remote mic off by default
        self._is_robot_talking = False  # For ducking (mic-specific)
        self._control_subs: list = []
        # Initialize logger wrapper (will be updated when set_logger is called)
        self.logger = UniversalLogger(enabled=False)

    def set_logger(self, logger):
        """Wrap the provided logger with UniversalLogger."""
        super().set_logger(logger)
        # Wrap the logger so we can call methods unconditionally
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)

    @property
    def name(self) -> str:
        return "micro"

    def initialize(self) -> bool:
        """Subscribe to the two Bool stt-control topics for the device's lifetime."""
        self._subscribe_controls()
        return True

    def set_tts_playing(self, is_playing: bool):
        """
        Called when TTS (text-to-speech) status changes.

        Implements "ducking" - suppressing mic input while robot speaks.

        Args:
            is_playing: True if robot is speaking, False otherwise
        """
        self._is_robot_talking = is_playing
        # Only the local stream is ducked: the on-robot mic hears the robot's own speaker,
        # but the operator's phone mic doesn't — ducking it would drop operator speech
        # whenever the robot talks.
        if self._local:
            self._local.set_ducking(is_playing)

    def on_open(self):
        """Start the local stream (the remote one follows only its own gate)."""
        if not self.proxy or not self.proxy.is_available():
            self.logger.error("❌ Proxy not configured - cannot start microphone input")
            return

        if self._local_enabled:
            self._start_local()

    def on_close(self):
        """Stop the local stream. The remote stream is deliberately left running:
        it is the operator's own phone mic, gated solely by /audio/remote_mic/stt,
        not by which directive (and thus which declared inputs) is active."""
        self._stop_local()

    def shutdown(self):
        """Tear down the streams and control subscriptions."""
        self._stop_local()
        self._stop_remote()
        for sub in self._control_subs:
            try:
                self.node.destroy_subscription(sub)
            except Exception:  # noqa: BLE001
                pass
        self._control_subs = []

    # --- control topics ---
    def _subscribe_controls(self):
        if self._control_subs or self.node is None:
            return
        from std_msgs.msg import Bool

        self._control_subs = [
            self.node.create_subscription(Bool, LOCAL_STT_TOPIC, self._on_local_toggle, 1),
            self.node.create_subscription(Bool, REMOTE_STT_TOPIC, self._on_remote_toggle, 1),
        ]
        self.logger.info(f"🎚️ stt control: {LOCAL_STT_TOPIC} (default on), {REMOTE_STT_TOPIC} (default off)")

    def _on_local_toggle(self, msg):
        enabled = bool(msg.data)
        if enabled == self._local_enabled:
            return
        self.logger.info(f"🎚️ local mic stt {'enabled' if enabled else 'disabled'}")
        self._local_enabled = enabled
        if not self.is_active():
            return  # device closed; applied on next on_open()
        self._start_local() if enabled else self._stop_local()

    def _on_remote_toggle(self, msg):
        enabled = bool(msg.data)
        if enabled == self._remote_enabled:
            return
        self.logger.info(f"🎚️ remote mic stt {'enabled' if enabled else 'disabled'}")
        self._remote_enabled = enabled
        # Operator's own phone mic: the Bool gate alone starts/stops it — no device-active
        # gate, so it works regardless of the running directive's declared inputs.
        self._start_remote() if enabled else self._stop_remote()

    # --- stream lifecycle ---
    def _start_local(self):
        if self._local:
            return
        self._local = self._make_stream("local", LocalMicStreamer(self.logger))

    def _stop_local(self):
        if self._local:
            self._local.stop()
            self._local = None

    def _start_remote(self):
        if self._remote:
            return
        if not self.proxy or not self.proxy.is_available():
            self.logger.error("❌ Proxy not configured - cannot start remote mic stream")
            return
        # is_active=True: transcripts flow whenever the gate is on, device active or not.
        self._remote = self._make_stream(
            "remote", RemoteMicStreamer(self.node, REMOTE_MIC_TOPIC, self.logger), is_active=lambda: True
        )

    def _stop_remote(self):
        if self._remote:
            self._remote.stop()
            self._remote = None

    def _make_stream(self, name: str, source, is_active=None) -> "TranscriptionStream | None":
        stream = TranscriptionStream(
            name=name,
            source=source,
            proxy=self.proxy,
            logger=self.logger,
            on_transcript=lambda text: self._on_transcript(text, name),
            is_active=is_active or self.is_active,
        )
        if name == "local":
            stream.set_ducking(self._is_robot_talking)
        try:
            stream.start()
            return stream
        except Exception as e:
            self.logger.error(f"❌ Failed to start {name} mic stream: {e}")
            import traceback

            traceback.print_exc()
            return None

    def _on_transcript(self, text: str, source: str):
        """Called when a transcript is ready from either stream."""
        if text:
            self.logger.info(f"🎤 [{source}] Transcript: {text}")
            self.send_data(text, data_type="chat_in")


# ========== Transcription stream ==========


class TranscriptionStream:
    """
    Streams one audio source to the OpenAI Realtime API and emits transcripts.

    Owns its own websocket client, audio-forwarding thread, and reconnect loop, so
    multiple sources (local mic, remote mic) transcribe independently. The source is
    any object exposing a `.queue` of raw 24 kHz PCM16 bytes plus `start()`/`stop()`.
    """

    def __init__(self, name, source, proxy, logger, on_transcript, is_active):
        self.name = name
        self._source = source
        self._proxy = proxy
        self.logger = logger
        self._on_transcript = on_transcript
        self._is_active = is_active
        self.client = None
        self._stop_evt = threading.Event()
        self._audio_thread = None
        self._reconnect_thread = None
        self._is_connected = False
        self._is_robot_talking = False
        self._reconnect_delay = 1  # Start with 1 second
        self._max_reconnect_delay = 30  # Max 30 seconds between retries

    def set_ducking(self, is_playing: bool):
        self._is_robot_talking = is_playing

    def start(self):
        """Start the audio source and connect to OpenAI."""
        self._stop_evt.clear()
        self._source.start()
        self._connect_via_proxy()

    def stop(self):
        """Stop the source, audio thread, and OpenAI client."""
        self._stop_evt.set()
        self._is_connected = False  # Prevent reconnection attempts

        if self._audio_thread:
            self._audio_thread.join(timeout=1.0)
        if self._reconnect_thread:
            self._reconnect_thread.join(timeout=1.0)

        try:
            self._source.stop()
        except Exception:  # noqa: BLE001
            pass

        if self.client:
            self.client.stop()
            self.client = None

    def _on_openai_message(self, ws, message: str):
        """Handle incoming messages from OpenAI Realtime API."""
        try:
            event = json.loads(message)
        except Exception:
            self.logger.error(f"Failed to parse message: {message[:200]}")
            return

        etype = event.get("type")

        # Event type to handler mapping
        event_handlers = {
            "session.updated": lambda e: self.logger.info(
                f"📋 [{self.name}] Session updated - transcription: "
                f"{e.get('session', {}).get('input_audio_transcription', {})}, "
                f"turn_detection: {e.get('session', {}).get('turn_detection', {})}"
            ),
            "input_audio_buffer.speech_started": lambda e: self.logger.info(f"🎤 [{self.name}] Speech detected"),
            "input_audio_buffer.speech_stopped": lambda e: self.logger.info(f"🔇 [{self.name}] Speech stopped"),
            "conversation.item.input_audio_transcription.completed": lambda e: (
                self._on_transcript(e.get("transcript", "")) if e.get("transcript") and self._is_active() else None
            ),
            "error": lambda e: (
                self.logger.error(
                    f"❌ [{self.name}] OpenAI error: {e.get('error', {}).get('code', '')} - "
                    f"{e.get('error', {}).get('message', '')} "
                    f"(param: {e.get('error', {}).get('param', '')})"
                )
                if e.get("error", {}).get("code") != "input_audio_buffer_commit_empty"
                else None
            ),
        }

        # Execute handler if exists, otherwise log unknown event type
        handler = event_handlers.get(etype)
        if handler:
            handler(event)
        else:
            self.logger.info(f"📨 [{self.name}] OpenAI event: {etype}")

    def _on_openai_open(self):
        """Handle WebSocket open event - send session configuration."""
        transcribe_model = self._proxy.config.get("openai_transcribe_model", "gpt-4o-mini-transcribe")
        vad_threshold = 0.3  # Lower = more sensitive to speech

        self.logger.info(f"📤 [{self.name}] WebSocket opened, sending session.update...")
        session_update = {
            "type": "session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": {"model": transcribe_model, "language": "en"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": float(vad_threshold),
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 700,
                    "create_response": False,
                },
                "instructions": "Transcribe user audio only in English; do not reply.",
            },
        }
        self.logger.info(f"📤 [{self.name}] Session config: model={transcribe_model}, vad_threshold={vad_threshold}")
        self.client.send_json(session_update)
        self.logger.info(f"📤 [{self.name}] session.update sent")

    def _on_openai_error(self, error):
        """Handle WebSocket error event."""
        self.logger.error(f"[{self.name}] [ws error] {error}")

    def _on_openai_close(self):
        """Handle WebSocket close event - trigger reconnection."""
        self.logger.warning(f"[{self.name}] WebSocket closed")
        self._is_connected = False

        # Don't reconnect if we're shutting down
        if self._stop_evt.is_set():
            return

        # Start reconnection in background thread
        self._schedule_reconnect()

    def _connect_via_proxy(self):
        """Connect to OpenAI Realtime API via proxy."""
        self.logger.info(f"🔗 [{self.name}] Connecting to OpenAI via proxy...")

        # Get config from proxy (injected by InputManager)
        model = self._proxy.config.get("openai_realtime_model", "gpt-4o-realtime-preview")

        # Use proxy's OpenAI adapter
        self.client = self._proxy.openai.realtime.connect_sync(
            model=model,
            on_message=self._on_openai_message,
            on_open=self._on_openai_open,
            on_error=self._on_openai_error,
            on_close=self._on_openai_close,
        )
        self.client.start()

        # Start audio streaming thread
        self._start_audio_thread()

        self._is_connected = True
        self._reconnect_delay = 1  # Reset delay on successful connection
        self.logger.info(f"✅ [{self.name}] Connected to OpenAI Realtime (model: {model})")

    def _schedule_reconnect(self):
        """Schedule a reconnection attempt in a background thread."""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return  # Already reconnecting

        def reconnect_loop():
            while not self._stop_evt.is_set() and not self._is_connected:
                self.logger.info(f"🔄 [{self.name}] Reconnecting to OpenAI in {self._reconnect_delay}s...")

                # Wait before reconnecting (interruptible)
                if self._stop_evt.wait(timeout=self._reconnect_delay):
                    break  # Stop event was set

                try:
                    # Stop old client if exists
                    if self.client:
                        try:
                            self.client.stop()
                        except:  # noqa: E722
                            pass
                        self.client = None

                    # Stop old audio thread
                    self._stop_evt.set()
                    if self._audio_thread and self._audio_thread.is_alive():
                        self._audio_thread.join(timeout=1.0)
                    self._stop_evt.clear()

                    # Reconnect
                    self._connect_via_proxy()

                    if self._is_connected:
                        self.logger.info(f"✅ [{self.name}] Reconnection successful!")
                        break

                except Exception as e:
                    self.logger.error(f"❌ [{self.name}] Reconnection failed: {e}")
                    # Exponential backoff
                    self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

        self._reconnect_thread = threading.Thread(target=reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _start_audio_thread(self):
        """Start the audio streaming thread."""
        self._stop_evt.clear()

        def audio_loop():
            if not self.client.wait_until_connected(timeout=10):
                self.logger.error(f"[{self.name}] WebSocket didn't connect in time")
                return

            self.logger.info(f"🎧 [{self.name}] Audio streaming thread started")

            chunks_sent = 0
            empty_count = 0
            ducking_logged = False
            while not self._stop_evt.is_set():
                try:
                    chunk = self._source.queue.get(timeout=0.1)
                    empty_count = 0  # Reset on successful get
                except queue.Empty:
                    empty_count += 1
                    if empty_count == 50:
                        self.logger.warning(f"⚠️ [{self.name}] No audio chunks received (queue empty for 5s)")
                    continue

                try:
                    # Skip sending if not connected (reconnection in progress)
                    if not self._is_connected:
                        continue

                    # Skip sending while ducking (robot is speaking)
                    if self._is_robot_talking:
                        if not ducking_logged:
                            self.logger.info(f"🔇 [{self.name}] Ducking active - not sending audio")
                        ducking_logged = True
                        continue
                    ducking_logged = False

                    payload = {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    }
                    self.client.send_json(payload)
                    chunks_sent += 1

                    # Log periodically (much less frequently)
                    if chunks_sent == 100:
                        self.logger.info(f"🎧 [{self.name}] Streaming audio ({chunks_sent} chunks)")
                    elif chunks_sent % 2500 == 0:
                        self.logger.info(f"🎧 [{self.name}] Audio chunks sent: {chunks_sent}")
                except Exception as e:
                    # Only log if we think we're connected (avoid spam during reconnect)
                    if self._is_connected:
                        self.logger.error(f"[{self.name}] Send error: {e}")

        self._audio_thread = threading.Thread(target=audio_loop, daemon=True)
        self._audio_thread.start()


# ========== Audio Streaming Helpers ==========


class LocalMicStreamer:
    """On-robot hardware mic: auto-detects an ALSA device and streams 24 kHz PCM via arecord."""

    def __init__(self, logger):
        self.logger = logger
        self._arecord = ArecordStreamer(logger)
        self.queue = self._arecord.queue

    def start(self):
        detected_device = self._detect_audio_device() or "default"
        self.logger.info(f"🎙️ Using audio device: {detected_device}")
        self._arecord.start(
            device=detected_device,
            sample_rate=DEFAULT_SAMPLE_RATE,
            channels=DEFAULT_CHANNELS,
        )
        self.logger.info(f"🎙️ Microphone started (rate: {DEFAULT_SAMPLE_RATE}, channels: {DEFAULT_CHANNELS})")

    def stop(self):
        self._arecord.stop()

    def _detect_audio_device(self):
        """Detect and list available audio capture devices."""
        devices = []
        try:
            result = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                pattern = r"card (\d+):.*?\[([^\]]+)\].*?device (\d+):"
                for match in re.finditer(pattern, result.stdout):
                    card_num = match.group(1)
                    card_name = match.group(2)
                    device_num = match.group(3)
                    device_id = f"plughw:{card_num},{device_num}"
                    devices.append({"card": card_num, "device": device_num, "name": card_name, "id": device_id})
        except Exception:
            pass

        # Try to find a suitable microphone device
        preferred_device = None

        self.logger.info(f"🔍 Found {len(devices)} audio devices: {[d['name'] for d in devices]}")

        # Look for USB microphones (usually better quality)
        for dev in devices:
            name_lower = dev["name"].lower()
            if "mic" in name_lower and "usb" in name_lower:
                preferred_device = dev
                break

        # Look for USB Sound Device (common external mic name)
        if not preferred_device:
            for dev in devices:
                name_lower = dev["name"].lower()
                if "sound" in name_lower and ("usb" in name_lower or "pnp" in name_lower):
                    preferred_device = dev
                    break

        # Fall back to any mic
        if not preferred_device:
            for dev in devices:
                if "mic" in dev["name"].lower():
                    preferred_device = dev
                    break

        # Fall back to any USB audio device (but NOT camera)
        if not preferred_device:
            for dev in devices:
                name_lower = dev["name"].lower()
                if "usb" in name_lower and "camera" not in name_lower and "webcam" not in name_lower:
                    preferred_device = dev
                    break

        # Fall back to camera audio (least preferred)
        if not preferred_device:
            for dev in devices:
                if "camera" in dev["name"].lower() or "webcam" in dev["name"].lower():
                    preferred_device = dev
                    break

        # Last resort: use first available device
        if not preferred_device and devices:
            preferred_device = devices[0]

        if preferred_device:
            self.logger.info(f"🎙️ Selected audio device: {preferred_device['name']} ({preferred_device['id']})")

        return preferred_device["id"] if preferred_device else None


class RemoteMicStreamer:
    """
    Inbound phone mic: subscribes to /audio/remote_mic (innate_audio/Audio) and exposes
    24 kHz PCM. The topic contract is S16LE mono 48 kHz; each message is downsampled to the
    24 kHz the OpenAI Realtime API expects. Same `.queue` contract as the local streamers.
    """

    def __init__(self, node, topic, logger):
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self._node = node
        self._topic = topic
        self.logger = logger
        self._sub = None
        self._ratecv_state = None  # audioop filter state, carried across chunks
        self._chunks = 0

    def start(self):
        # Imported lazily: the remote path is off by default and innate_audio may be absent.
        from innate_audio.msg import Audio
        from rclpy.qos import qos_profile_sensor_data

        self._sub = self._node.create_subscription(Audio, self._topic, self._on_audio, qos_profile_sensor_data)
        self.logger.info(f"🎙️ Subscribed to remote mic topic: {self._topic}")

    def _on_audio(self, msg):
        pcm = array.array("h", msg.samples).tobytes()
        if msg.rate != DEFAULT_SAMPLE_RATE:
            pcm, self._ratecv_state = audioop.ratecv(
                pcm, 2, msg.channels or DEFAULT_CHANNELS, msg.rate, DEFAULT_SAMPLE_RATE, self._ratecv_state
            )
        try:
            self.queue.put_nowait(pcm)
            self._chunks += 1
            if self._chunks == 1:
                self.logger.info(f"🎙️ First remote-mic audio ({len(pcm)} bytes @ {DEFAULT_SAMPLE_RATE}Hz)")
        except queue.Full:
            pass

    def stop(self):
        if self._sub is not None and self._node is not None:
            try:
                self._node.destroy_subscription(self._sub)
            except Exception:  # noqa: BLE001
                pass
            self._sub = None


class ArecordStreamer:
    """Streams audio from ALSA via arecord subprocess."""

    def __init__(self, logger):
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self._proc: subprocess.Popen | None = None
        self.logger = logger
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.channels = DEFAULT_CHANNELS
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, device: str = "default", sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        # arecord raw PCM 16-bit, stdout
        cmd = [
            "arecord",
            "-D",
            str(device),
            "-f",
            "S16_LE",
            "-r",
            str(self.sample_rate),
            "-c",
            str(self.channels),
            "-t",
            "raw",
            "-q",  # quiet
            "-",
        ]
        self.logger.info(f"🎙️ Starting arecord: {' '.join(cmd)}")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        if not self._proc or not self._proc.stdout:
            raise RuntimeError("Failed to start arecord process")
        self.logger.info(f"🎙️ arecord process started (pid: {self._proc.pid})")

        def reader():
            try:
                bytes_per_sample = 2
                frame_bytes = int(self.sample_rate * CHUNK_DURATION_SEC * self.channels * bytes_per_sample)
                self.logger.info(f"🎙️ Reader thread started, reading {frame_bytes} bytes per chunk")
                chunks_read = 0
                while not self._stop.is_set():
                    buf = self._proc.stdout.read(frame_bytes)
                    if not buf:
                        # Check if process died
                        if self._proc.poll() is not None:
                            stderr = self._proc.stderr.read().decode() if self._proc.stderr else ""
                            self.logger.error(f"❌ arecord died with code {self._proc.returncode}: {stderr}")
                            break
                        time.sleep(0.01)
                        continue
                    chunks_read += 1
                    if chunks_read == 1:
                        self.logger.info(f"🎙️ First audio chunk received ({len(buf)} bytes)")
                    try:
                        self.queue.put_nowait(buf)
                    except queue.Full:
                        pass
            except Exception as e:
                self.logger.error(f"arecord reader error: {e}")

        self._reader_thread = threading.Thread(target=reader, daemon=True)
        self._reader_thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._reader_thread:
                self._reader_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            if self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.0)
                except Exception:
                    self._proc.kill()
        except Exception:
            pass


class MicStreamer:
    """Streams audio via sounddevice (PortAudio)."""

    def __init__(self, logger):
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=50)
        self._stream: sd.RawInputStream | None = None
        self.logger = logger
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.channels = DEFAULT_CHANNELS

    def _callback(self, indata, frames, time_info, status):
        if status:
            self.logger.warn(f"[PortAudio] {status}")
        try:
            self.queue.put_nowait(bytes(indata))
        except queue.Full:
            pass

    def start(
        self, device: str | None = None, sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS
    ):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        frames_per_chunk = int(self.sample_rate * CHUNK_DURATION_SEC)
        kwargs = dict(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=DTYPE,
            blocksize=frames_per_chunk,
            callback=self._callback,
        )
        if device:
            try:
                kwargs["device"] = int(device) if isinstance(device, str) and device.isdigit() else device
            except Exception:
                kwargs["device"] = device

        self._stream = sd.RawInputStream(**kwargs)
        self._stream.start()

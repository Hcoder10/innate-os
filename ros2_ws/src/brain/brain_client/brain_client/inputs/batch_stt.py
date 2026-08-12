# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Batch speech-to-text: local endpointing, one blocking API call per utterance.

The realtime backends (Scribe, OpenAI) do voice-activity detection server-side;
a batch backend has no session to do it in, so the endpointing moves onto the
robot. :class:`Endpointer` runs a voiced/unvoiced detector — Silero VAD
(``brain_client.inputs.vad``) or the energy fallback — over the PCM stream and
closes an utterance after enough silence; :class:`BatchSttSession` then ships
the whole clip as WAV through a vendor transcriber — ElevenLabs Scribe batch or
Gemini ``generateContent``.
"""

from __future__ import annotations

import array
import base64
import io
import json
import math
import queue
import threading
import wave
from collections import deque
from typing import TYPE_CHECKING, Any

from brain_client.brain.transport import GENERATE_PATH

if TYPE_CHECKING:
    from collections.abc import Callable

    from brain_client.brain.transport import GeminiRest
    from brain_client.common.logging import UniversalLogger
    from innate_proxy import ProxyClient

    Transcriber = Callable[[bytes], str]
    """WAV bytes -> transcript text; "" when nothing was said."""

    VoicedDetector = Callable[[bytes], bool]
    """One PCM chunk -> does it contain speech?"""

PRE_ROLL_SECS = 0.4
MAX_UTTERANCE_SECS = 30.0
MIN_VOICED_SECS = 0.25


def rms_level(chunk: bytes) -> float:
    """Normalized RMS (0..1) of a 16-bit mono PCM chunk."""
    samples = array.array("h", chunk)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


class EnergyDetector:
    """Voiced = RMS above a fixed threshold; `level` is the last chunk's RMS."""

    def __init__(self, threshold: float):
        self.threshold = threshold
        self.level = 0.0
        self.voiced = False

    def __call__(self, chunk: bytes) -> bool:
        self.level = rms_level(chunk)
        self.voiced = self.level >= self.threshold
        return self.voiced


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


class Endpointer:
    """Cuts a continuous PCM stream into utterances by a voiced/unvoiced detector.

    Time is measured in audio fed, not wall clock — ducking pauses the stream,
    and a paused stream must not count as silence.
    """

    def __init__(self, *, sample_rate: int, is_voiced: VoicedDetector, silence_secs: float):
        self._is_voiced = is_voiced
        self._silence_secs = silence_secs
        self._bytes_per_sec = sample_rate * 2
        self._pre_roll: deque[bytes] = deque()
        self._pre_roll_bytes = 0
        self._utterance = bytearray()
        self._in_speech = False
        self._silence_bytes = 0
        self._voiced_bytes = 0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def utterance_secs(self) -> float:
        return len(self._utterance) / self._bytes_per_sec

    def feed(self, chunk: bytes) -> bytes | None:
        """Consume one chunk; returns the finished utterance's PCM when one closes."""
        voiced = self._is_voiced(chunk)

        if not self._in_speech:
            self._buffer_pre_roll(chunk)
            if not voiced:
                return None
            self._in_speech = True
            self._utterance = bytearray(b"".join(self._pre_roll))
            self._silence_bytes = 0
            self._voiced_bytes = len(chunk)
            return None

        self._utterance.extend(chunk)
        if voiced:
            self._silence_bytes = 0
            self._voiced_bytes += len(chunk)
        else:
            self._silence_bytes += len(chunk)

        trailing_silence = self._silence_bytes / self._bytes_per_sec
        utterance_secs = len(self._utterance) / self._bytes_per_sec
        if trailing_silence < self._silence_secs and utterance_secs < MAX_UTTERANCE_SECS:
            return None
        return self._close()

    def _buffer_pre_roll(self, chunk: bytes) -> None:
        self._pre_roll.append(chunk)
        self._pre_roll_bytes += len(chunk)
        while self._pre_roll_bytes > PRE_ROLL_SECS * self._bytes_per_sec:
            self._pre_roll_bytes -= len(self._pre_roll.popleft())

    def _close(self) -> bytes | None:
        utterance = bytes(self._utterance)
        long_enough = self._voiced_bytes / self._bytes_per_sec >= MIN_VOICED_SECS
        self._in_speech = False
        self._utterance = bytearray()
        self._pre_roll.clear()
        self._pre_roll_bytes = 0
        self._voiced_bytes = 0
        self._silence_bytes = 0
        return utterance if long_enough else None


# ---------- ElevenLabs Scribe batch ----------

ELEVENLABS_PROXY_ENDPOINT = "v1/speech-to-text"


def elevenlabs_proxy_transcriber(proxy: ProxyClient, model: str, language: str) -> Transcriber:
    def transcribe(wav: bytes) -> str:
        files = {"file": ("utterance.wav", wav, "audio/wav")}
        form = {"model_id": model, "language_code": language}
        with proxy.request_stream("elevenlabs", ELEVENLABS_PROXY_ENDPOINT, files=files, form=form) as resp:
            payload = resp.read()
            if resp.status_code != 200:
                raise RuntimeError(f"elevenlabs via proxy: HTTP {resp.status_code}: {payload[:200]!r}")
            return str(json.loads(payload).get("text", "")).strip()

    return transcribe


# ---------- Gemini ----------

# The model must have an unambiguous way to say "nothing was said" — an empty
# reply can't be distinguished from a refusal or a formatting quirk.
NO_SPEECH = "NO_SPEECH"

_GEMINI_PROMPT = (
    "Transcribe the speech in this audio verbatim. Reply with only the "
    "transcript text - no quotes, labels, or commentary. The speaker's "
    "language is most likely {language}. If the audio contains no "
    f"intelligible human speech, reply with exactly {NO_SPEECH}."
)


def gemini_transcriber(rest: GeminiRest, model: str, language: str) -> Transcriber:
    def transcribe(wav: bytes) -> str:
        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(wav).decode()}},
                        {"text": _GEMINI_PROMPT.format(language=language)},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0, "thinkingConfig": {"thinkingLevel": "minimal"}},
        }
        response = rest.post(GENERATE_PATH.format(model=model), body)
        parts = response.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        return "" if text == NO_SPEECH else text

    return transcribe


# ---------- Session ----------


class BatchSttSession:
    """Feeds mic chunks to the endpointer and transcribes closed utterances.

    Lifecycle-compatible with the realtime WebSocket clients (start / stop /
    wait_until_connected) so MicroInput drives every backend the same way.
    Transcription is blocking HTTP, so it runs on its own worker thread —
    the mic feed must never stall behind a slow call.
    """

    def __init__(
        self,
        *,
        transcriber: Transcriber,
        sample_rate: int,
        is_voiced: VoicedDetector,
        silence_secs: float,
        on_transcript: Callable[[str], None],
        logger: UniversalLogger,
    ):
        self._transcriber = transcriber
        self._sample_rate = sample_rate
        self._on_transcript = on_transcript
        self._logger = logger
        self._endpointer = Endpointer(sample_rate=sample_rate, is_voiced=is_voiced, silence_secs=silence_secs)
        self._utterances: queue.Queue[bytes | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self.utterance_count = 0
        self.failure_count = 0

    def start(self) -> None:
        self._worker = threading.Thread(target=self._transcribe_loop, daemon=True)
        self._worker.start()

    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        return True

    def feed(self, chunk: bytes) -> None:
        utterance = self._endpointer.feed(chunk)
        if utterance is not None:
            self._logger.info(f"🎤 Utterance closed ({len(utterance) / (self._sample_rate * 2):.1f}s), transcribing...")
            self.utterance_count += 1
            self._utterances.put(utterance)

    def status(self) -> dict[str, Any]:
        return {
            "utterance_open": self._endpointer.in_speech,
            "utterance_secs": round(self._endpointer.utterance_secs, 2),
            "utterances": self.utterance_count,
            "failures": self.failure_count,
        }

    def stop(self) -> None:
        self._utterances.put(None)
        if self._worker:
            self._worker.join(timeout=2.0)
            self._worker = None

    def _transcribe_loop(self) -> None:
        while True:
            utterance = self._utterances.get()
            if utterance is None:
                return
            try:
                text = self._transcriber(pcm_to_wav(utterance, self._sample_rate))
            except Exception as e:  # noqa: BLE001 — one failed call must not kill the mic
                self.failure_count += 1
                self._logger.error(f"❌ Batch transcription failed: {e}")
                continue
            if text:
                self._on_transcript(text)

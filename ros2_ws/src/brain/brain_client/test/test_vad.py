# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the Silero VAD detector (no ROS, no network).

The windowing/hysteresis state machine is driven with a scripted fake model.
The vendored ONNX model itself runs against real audio when onnxruntime is
installed: silence and white noise must stay below any usable threshold, and
the recorded utterance in assets/ must be detected — that clip is what catches
a broken model interface (e.g. dropping the 64-sample context prefix), which
no synthetic signal can: Silero scores synthetic audio near zero either way.
"""

import wave
from pathlib import Path

import numpy as np
import pytest

from brain_client.inputs.batch_stt import Endpointer
from brain_client.inputs.vad import (
    WINDOW_SAMPLES,
    SileroDetector,
    SileroModel,
    pcm16_to_f32,
    resample_24k_to_16k,
    silero_detector,
)

SPEECH_WAV = Path(__file__).parent / "assets" / "utterance_24k.wav"

MIC_CHUNK = b"\x00\x00" * 480  # 20 ms at 24 kHz -> 320 samples at 16 kHz
WINDOW_CHUNK = b"\x00\x00" * 768  # exactly one 512-sample model window


class ScriptedModel:
    def __init__(self, probs):
        self.probs = list(probs)
        self.calls = 0

    def __call__(self, window) -> float:
        assert len(window) == WINDOW_SAMPLES
        prob = self.probs[self.calls]
        self.calls += 1
        return prob


class CapturingLogger:
    def __init__(self):
        self.errors = []

    def info(self, msg):
        pass

    def error(self, msg):
        self.errors.append(msg)


# ---------- resampler ----------


def test_resample_ratio_and_values():
    samples = np.array([3.0, 1.0, 5.0, 6.0, 0.0, 0.0], dtype=np.float32)
    out = resample_24k_to_16k(samples)
    assert out.tolist() == [3.0, 3.0, 6.0, 0.0]  # first of each triplet, midpoint of the rest


def test_resample_drops_partial_triplet():
    assert len(resample_24k_to_16k(np.zeros(481, dtype=np.float32))) == 320


def test_pcm16_to_f32_scales_to_unit_range():
    pcm = np.array([-32768, 0, 16384], dtype=np.int16).tobytes()
    assert pcm16_to_f32(pcm).tolist() == [-1.0, 0.0, 0.5]


def test_pcm16_to_f32_ignores_a_trailing_odd_byte():
    """arecord's pipe is raw (bufsize=0), so a read can end mid-frame."""
    pcm = np.array([-32768, 0, 16384], dtype=np.int16).tobytes() + b"\x01"
    assert pcm16_to_f32(pcm).tolist() == [-1.0, 0.0, 0.5]


# ---------- detector state machine ----------


def test_detector_runs_model_at_window_cadence():
    model = ScriptedModel([0.0] * 10)
    detector = SileroDetector(model, threshold=0.5)
    calls_after_chunk = []
    for _ in range(5):
        detector(MIC_CHUNK)
        calls_after_chunk.append(model.calls)
    # 320 resampled samples per chunk against 512-sample windows
    assert calls_after_chunk == [0, 1, 1, 2, 3]


def test_detector_enter_exit_hysteresis():
    # threshold 0.5 -> exits at 0.35: 0.4 keeps speech open, 0.3 closes it
    model = ScriptedModel([0.6, 0.4, 0.4, 0.3])
    detector = SileroDetector(model, threshold=0.5)
    assert [detector(WINDOW_CHUNK) for _ in range(4)] == [True, True, True, False]


def test_detector_decision_sticky_between_windows():
    model = ScriptedModel([0.9])
    detector = SileroDetector(model, threshold=0.5)
    detector(WINDOW_CHUNK)
    assert detector(MIC_CHUNK) is True  # no window completed — last decision holds
    assert model.calls == 1


# ---------- factory fallback ----------


def test_factory_rejects_wrong_sample_rate():
    logger = CapturingLogger()
    assert silero_detector(0.3, 48_000, logger) is None
    assert "48000" in logger.errors[0]


def test_factory_returns_none_when_model_unloadable():
    logger = CapturingLogger()
    assert silero_detector(0.3, 24_000, logger, model_path=Path("/nonexistent.onnx")) is None
    assert len(logger.errors) == 1


# ---------- vendored model smoke ----------


def test_real_model_rejects_silence_and_noise():
    pytest.importorskip("onnxruntime")
    model = SileroModel()

    assert model(np.zeros(WINDOW_SAMPLES, dtype=np.float32)) < 0.1

    rng = np.random.default_rng(0)
    noise_probs = [model(rng.normal(0, 0.1, WINDOW_SAMPLES).astype(np.float32)) for _ in range(10)]
    assert max(noise_probs) < 0.3


def test_real_detector_stays_unvoiced_through_silence():
    pytest.importorskip("onnxruntime")
    logger = CapturingLogger()
    detector = silero_detector(0.3, 24_000, logger)
    assert detector is not None
    assert not any(detector(MIC_CHUNK) for _ in range(50))  # 1 s of silence
    assert logger.errors == []


def test_real_detector_endpoints_recorded_speech():
    pytest.importorskip("onnxruntime")
    with wave.open(str(SPEECH_WAV), "rb") as w:
        assert w.getframerate() == 24_000 and w.getnchannels() == 1
        speech = w.readframes(w.getnframes())

    detector = silero_detector(0.3, 24_000, CapturingLogger())
    assert detector is not None
    ep = Endpointer(sample_rate=24_000, is_voiced=detector, silence_secs=0.3)

    stream = speech + b"\x00\x00" * 24_000  # 1 s of trailing silence to close
    chunk = 480 * 2
    utterances = [u for i in range(0, len(stream) - chunk, chunk) if (u := ep.feed(stream[i : i + chunk]))]

    assert len(utterances) == 1
    # the clip is ~2.8 s of speech; the utterance adds pre-roll and the close window
    assert 2.5 <= len(utterances[0]) / (24_000 * 2) <= 4.0

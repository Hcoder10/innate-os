# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Silero VAD: neural voiced/unvoiced decisions for the batch STT endpointer.

An energy threshold cannot tell speech from fan or motor noise; Silero can, at
~0.7 ms per 32 ms window on the Orin CPU. The vendored model is Silero VAD
v6.2.1 (MIT, github.com/snakers4/silero-vad,
sha256 1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable

    from brain_client.common.logging import UniversalLogger

MODEL_PATH = Path(__file__).with_name("silero_vad.onnx")
MODEL_SAMPLE_RATE = 16_000
MIC_SAMPLE_RATE = 24_000
WINDOW_SAMPLES = 512
# The graph accepts any window length but was trained on 576 = 64 samples of the
# previous window + 512 new; bare 512-sample windows score ~0.003 on real speech.
CONTEXT_SAMPLES = 64
# Silero's own VADIterator exits speech this far below the entry threshold.
EXIT_THRESHOLD_DELTA = 0.15


def pcm16_to_f32(chunk: bytes) -> np.ndarray:
    """A trailing odd byte is a partial frame from the capture pipe, not a sample."""
    return np.frombuffer(chunk[: len(chunk) - len(chunk) % 2], dtype=np.int16).astype(np.float32) / 32768.0


def resample_24k_to_16k(samples: np.ndarray) -> np.ndarray:
    """3:2 linear resample: each triplet keeps its first sample and the midpoint of the other two."""
    samples = samples[: len(samples) - len(samples) % 3]
    triplets = samples.reshape(-1, 3)
    out = np.empty((len(triplets), 2), dtype=np.float32)
    out[:, 0] = triplets[:, 0]
    out[:, 1] = (triplets[:, 1] + triplets[:, 2]) * 0.5
    return out.reshape(-1)


class SileroModel:
    """One ONNX inference session mapping a 512-sample 16 kHz window to speech probability."""

    def __init__(self, model_path: Path = MODEL_PATH):
        import onnxruntime  # deferred: the energy fallback must not pay this import

        opts = onnxruntime.SessionOptions()
        # single-threaded on purpose: the model runs in ~0.7 ms, and input_manager
        # shares the Jetson with vision and nav — no thread pool worth spinning up
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._sr = np.array(MODEL_SAMPLE_RATE, dtype=np.int64)

    def __call__(self, window: np.ndarray) -> float:
        contexted = np.concatenate([self._context, window]).reshape(1, -1)
        outputs = self._session.run(None, {"input": contexted, "state": self._state, "sr": self._sr})
        prob, self._state = np.asarray(outputs[0]), np.asarray(outputs[1])
        self._context = window[-CONTEXT_SAMPLES:]
        return float(prob[0, 0])

    def reset(self) -> None:
        """Zero the RNN state and context: a new audio stream must not score its
        first windows against the previous stream's hidden state (measured: stale
        state marks ~0.3 s of room tone as speech — a phantom utterance)."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)


class SileroDetector:
    """Chunk-level voiced decision: resample mic PCM to the model rate, window, hysteresis.

    Mic chunks are 20 ms and model windows 32 ms, so the decision is sticky
    between windows; speech ends only when the probability falls
    EXIT_THRESHOLD_DELTA below the entry threshold.
    """

    def __init__(self, model: Callable[[np.ndarray], float], threshold: float):
        self._model = model
        self.threshold = threshold
        self.level = 0.0
        self._exit = max(threshold - EXIT_THRESHOLD_DELTA, 0.01)
        self._buffer = np.empty(0, dtype=np.float32)
        self.voiced = False

    def __call__(self, chunk: bytes) -> bool:
        self._buffer = np.concatenate([self._buffer, resample_24k_to_16k(pcm16_to_f32(chunk))])
        while len(self._buffer) >= WINDOW_SAMPLES:
            window, self._buffer = self._buffer[:WINDOW_SAMPLES], self._buffer[WINDOW_SAMPLES:]
            self.level = self._model(window)
            self.voiced = self.level >= (self._exit if self.voiced else self.threshold)
        return self.voiced


@lru_cache(maxsize=1)
def _shared_model(model_path: Path) -> SileroModel:
    """One ONNX session per process: the reconnect loop rebuilds detectors, and
    the session build is the expensive part (a raise is never cached, so the
    energy fallback still gets retried construction every connect)."""
    return SileroModel(model_path)


def silero_detector(
    threshold: float, sample_rate: int, logger: UniversalLogger, model_path: Path = MODEL_PATH
) -> SileroDetector | None:
    """Build the Silero chunk detector, or None (reason logged) so the caller can fall back."""
    if sample_rate != MIC_SAMPLE_RATE:
        logger.error(f"Silero VAD needs {MIC_SAMPLE_RATE} Hz input, got {sample_rate} — falling back to energy VAD")
        return None
    try:
        model = _shared_model(model_path)
        model.reset()  # the cached session still carries the last stream's RNN state
    except Exception as e:  # noqa: BLE001 — a missing runtime must degrade to energy VAD, not kill the mic
        logger.error(f"Silero VAD unavailable ({e}) — falling back to energy VAD")
        return None
    return SileroDetector(model, threshold)

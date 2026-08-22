# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Small deterministic checks for simulator speech timing."""

import io
import wave

import pytest

from brain_client.transport.tts import _wav_duration_s


def test_wav_duration_matches_pcm_frames():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 8_000)

    assert _wav_duration_s(buf.getvalue()) == pytest.approx(0.5)


def test_wav_duration_rejects_invalid_audio():
    assert _wav_duration_s(b"not a wav") == 0.0

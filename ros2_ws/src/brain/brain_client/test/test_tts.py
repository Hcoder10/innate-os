# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Small deterministic checks for simulator speech timing and queue policy."""

import io
import wave

import pytest

from brain_client.transport.tts import _Utterance, _survives_flush, _wav_duration_s


def _utterance(reply_id=None, protected=False):
    return _Utterance("some words", None, None, reply_id, protected)


def test_flush_keeps_rest_of_the_reply_being_spoken():
    # Reply-1's first sentence is playing; its second sentence must not be
    # dropped by a newer reply's flush — the reply holds the floor.
    assert _survives_flush(_utterance(reply_id="reply-1"), "reply-1")


def test_flush_drops_replies_that_never_started():
    assert not _survives_flush(_utterance(reply_id="reply-2"), "reply-1")


def test_flush_drops_untagged_backlog():
    assert not _survives_flush(_utterance(), None)
    assert not _survives_flush(_utterance(), "reply-1")


def test_flush_spares_protected_environment_speech():
    assert _survives_flush(_utterance(protected=True), "reply-1")
    assert _survives_flush(_utterance(protected=True), None)


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

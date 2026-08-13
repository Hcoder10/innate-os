# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the batch STT backends (no ROS, no network).

The endpointer is a pure state machine over PCM chunks, so it's driven with
synthetic audio; each vendor transcriber is exercised against a capturing fake
asserting the exact request shape.
"""

import base64
import io
import json
import threading
import wave

import httpx
import pytest

from brain_client.brain.transport import GeminiRest
from brain_client.inputs.batch_stt import (
    ELEVENLABS_PROXY_ENDPOINT,
    MAX_PENDING_UTTERANCES,
    MAX_UTTERANCE_SECS,
    NO_SPEECH,
    TRANSCRIBE_TIMEOUT_SECS,
    BatchSttSession,
    Endpointer,
    EnergyDetector,
    elevenlabs_proxy_transcriber,
    gemini_transcriber,
    pcm_to_wav,
    rms_level,
)

SAMPLE_RATE = 24_000
CHUNK_SAMPLES = 480  # 20 ms

LOUD = (b"\x40\x1f" + b"\xc0\xe0") * (CHUNK_SAMPLES // 2)  # ±8000 square wave, RMS ~0.24
QUIET = b"\x00\x00" * CHUNK_SAMPLES

WAV = pcm_to_wav(LOUD * 10, SAMPLE_RATE)


def make_endpointer(silence_secs: float = 0.3) -> Endpointer:
    return Endpointer(sample_rate=SAMPLE_RATE, is_voiced=EnergyDetector(0.01), silence_secs=silence_secs)


def feed_seconds(ep: Endpointer, chunk: bytes, seconds: float) -> list[bytes]:
    utterances = []
    for _ in range(int(seconds * 50)):
        utt = ep.feed(chunk)
        if utt is not None:
            utterances.append(utt)
    return utterances


class NullLogger:
    def info(self, msg):
        pass

    def error(self, msg):
        pass


# ---------- energy ----------


def test_rms_level_scales():
    assert rms_level(QUIET) == 0.0
    assert 0.2 < rms_level(LOUD) < 0.3


def test_energy_detector_tracks_level():
    detector = EnergyDetector(0.01)
    assert detector(LOUD) is True and detector.level > 0.2
    assert detector(QUIET) is False and detector.level == 0.0


# ---------- endpointer ----------


def test_utterance_closes_after_silence_and_includes_pre_roll():
    ep = make_endpointer(silence_secs=0.3)
    feed_seconds(ep, QUIET, 1.0)
    assert feed_seconds(ep, LOUD, 1.0) == []
    utterances = feed_seconds(ep, QUIET, 0.5)

    assert len(utterances) == 1
    # ~0.4s pre-roll + 1.0s speech + 0.3s trailing silence (±1 chunk of trim slack)
    assert 1.6 <= len(utterances[0]) / (SAMPLE_RATE * 2) <= 1.75


def test_short_blip_is_dropped():
    ep = make_endpointer(silence_secs=0.3)
    ep.feed(LOUD)  # 20 ms of "speech" — far under MIN_VOICED_SECS
    assert feed_seconds(ep, QUIET, 1.0) == []


def test_long_utterance_capped():
    ep = make_endpointer(silence_secs=0.3)
    utterances = feed_seconds(ep, LOUD, MAX_UTTERANCE_SECS + 2)
    assert len(utterances) >= 1
    assert len(utterances[0]) >= int(MAX_UTTERANCE_SECS * SAMPLE_RATE) * 2


def test_endpointer_resets_between_utterances():
    ep = make_endpointer(silence_secs=0.3)
    feed_seconds(ep, LOUD, 1.0)
    first = feed_seconds(ep, QUIET, 0.5)
    feed_seconds(ep, LOUD, 0.5)
    second = feed_seconds(ep, QUIET, 0.5)

    assert len(first) == len(second) == 1
    assert len(second[0]) < len(first[0])  # no leftover audio from the first


def test_ducking_gap_does_not_count_as_silence():
    # Time is audio fed, not wall clock: with no chunks arriving, the
    # utterance must stay open no matter how long the gap.
    ep = make_endpointer(silence_secs=0.3)
    feed_seconds(ep, LOUD, 0.5)
    assert ep.feed(LOUD) is None  # still open after an arbitrary pause


def test_endpointer_survives_a_partial_frame():
    """arecord's pipe is raw (bufsize=0), so a read can return an odd byte count.
    The reader realigns, but int16 parsing must not explode if one slips through."""
    assert make_endpointer().feed(b"\x01" * 333) is None


# ---------- wav ----------


def test_pcm_to_wav_round_trip():
    pcm = LOUD * 50
    with wave.open(io.BytesIO(pcm_to_wav(pcm, SAMPLE_RATE)), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.readframes(wav.getnframes()) == pcm


# ---------- elevenlabs transcriber ----------


class FakeProxyResponse:
    def __init__(self, status_code: int, payload: bytes):
        self.status_code = status_code
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "FakeProxyResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class FakeProxy:
    """Capturing ProxyClient stand-in — a fresh one-shot response per call, like the real stream()."""

    def __init__(self, status_code: int = 200, payload: bytes = b"{}"):
        self.calls: list[tuple[str, str, dict]] = []
        self._status_code = status_code
        self._payload = payload

    def request_stream(self, service: str, endpoint: str, **kwargs: object) -> FakeProxyResponse:
        self.calls.append((service, endpoint, kwargs))
        return FakeProxyResponse(self._status_code, self._payload)


def test_elevenlabs_proxy_request_shape_and_reply():
    proxy = FakeProxy(payload=json.dumps({"text": " via proxy "}).encode())
    transcribe = elevenlabs_proxy_transcriber(proxy, "scribe_v2", "en")

    assert transcribe(WAV) == "via proxy"
    service, endpoint, kwargs = proxy.calls[0]
    assert service == "elevenlabs"
    assert endpoint == ELEVENLABS_PROXY_ENDPOINT
    assert kwargs["files"]["file"] == ("utterance.wav", WAV, "audio/wav")
    assert kwargs["form"] == {"model_id": "scribe_v2", "language_code": "en"}
    assert kwargs["timeout"] == TRANSCRIBE_TIMEOUT_SECS


def test_elevenlabs_proxy_http_error_raises():
    transcribe = elevenlabs_proxy_transcriber(FakeProxy(status_code=401, payload=b"denied"), "scribe_v2", "en")
    try:
        transcribe(WAV)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "401" in str(e)


def test_elevenlabs_proxy_wire_shape_through_real_client():
    """End to end through a real ProxyClient: the multipart body that leaves
    the robot must carry the WAV and both form fields."""
    innate_proxy = pytest.importorskip("innate_proxy")

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": " hello robot "})

    proxy = innate_proxy.ProxyClient(proxy_url="https://proxy.test", innate_service_key="test-key")
    proxy._sync_client = httpx.Client(transport=httpx.MockTransport(handler))

    assert elevenlabs_proxy_transcriber(proxy, "scribe_v2", "en")(WAV) == "hello robot"

    request = requests[0]
    assert request.url == "https://proxy.test/v1/services/elevenlabs/v1/speech-to-text"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.read()
    assert WAV in body
    assert b'name="model_id"' in body and b"scribe_v2" in body
    assert b'name="language_code"' in body


# ---------- gemini transcriber ----------


def fake_rest(reply_text: str, calls: list | None = None) -> GeminiRest:
    def post(path, body):
        if calls is not None:
            calls.append((path, body))
        return {"candidates": [{"content": {"parts": [{"text": reply_text}]}}]}

    return GeminiRest(post=post, delete=lambda path: {}, upload=lambda path, data, mime: {})


def test_gemini_request_shape_and_reply():
    calls = []
    transcribe = gemini_transcriber(fake_rest("hello robot", calls), "gemini-3.6-flash", "en")

    assert transcribe(WAV) == "hello robot"
    path, body = calls[0]
    assert path == "/v1beta/models/gemini-3.6-flash:generateContent"
    audio_part, text_part = body["contents"][0]["parts"]
    assert audio_part["inlineData"]["mimeType"] == "audio/wav"
    assert base64.b64decode(audio_part["inlineData"]["data"]) == WAV
    assert "en" in text_part["text"]
    assert body["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "minimal"


def test_gemini_no_speech_sentinel_maps_to_empty():
    assert gemini_transcriber(fake_rest(NO_SPEECH), "gemini-3.6-flash", "en")(b"wav") == ""


def test_gemini_empty_candidates():
    rest = GeminiRest(post=lambda path, body: {}, delete=lambda p: {}, upload=lambda p, d, m: {})
    assert gemini_transcriber(rest, "gemini-3.6-flash", "en")(b"wav") == ""


# ---------- session ----------


def make_session(transcriber, transcripts: list, done: threading.Event) -> BatchSttSession:
    def on_transcript(text):
        transcripts.append(text)
        done.set()

    return BatchSttSession(
        transcriber=transcriber,
        sample_rate=SAMPLE_RATE,
        is_voiced=EnergyDetector(0.01),
        silence_secs=0.3,
        on_transcript=on_transcript,
        logger=NullLogger(),
    )


def feed_utterance(session: BatchSttSession):
    for _ in range(50):
        session.feed(LOUD)
    for _ in range(25):
        session.feed(QUIET)


def test_session_transcribes_closed_utterance():
    transcripts, done = [], threading.Event()
    session = make_session(lambda wav: "bring me the sock", transcripts, done)
    session.start()
    try:
        feed_utterance(session)
        assert done.wait(timeout=2.0)
    finally:
        session.stop()
    assert transcripts == ["bring me the sock"]
    assert session.status() == {"utterance_open": False, "utterance_secs": 0.0, "utterances": 1, "failures": 0}


def test_session_status_tracks_open_utterance():
    session = make_session(lambda wav: "", [], threading.Event())
    for _ in range(50):
        session.feed(LOUD)
    status = session.status()
    assert status["utterance_open"] is True
    assert status["utterance_secs"] > 0.9  # ~1 s fed + pre-roll


def test_session_backlog_bounded_drops_oldest():
    """With the worker stalled, only the newest utterances stay pending —
    a shadow endpointer fed identically predicts the survivors exactly."""
    session = make_session(lambda wav: "", [], threading.Event())
    shadow = make_endpointer()
    fed: list[bytes] = []
    for i in range(MAX_PENDING_UTTERANCES + 2):
        for chunk in [LOUD] * (30 + 10 * i) + [QUIET] * 25:
            session.feed(chunk)
            if (utterance := shadow.feed(chunk)) is not None:
                fed.append(utterance)

    assert len(fed) == MAX_PENDING_UTTERANCES + 2
    assert session.utterance_count == MAX_PENDING_UTTERANCES + 2
    assert list(session._utterances.queue) == fed[-MAX_PENDING_UTTERANCES:]


def test_session_survives_transcription_error():
    calls = []

    def transcriber(wav):
        calls.append(wav)
        if len(calls) == 1:
            raise RuntimeError("HTTP 500")
        return "second try"

    transcripts, done = [], threading.Event()
    session = make_session(transcriber, transcripts, done)

    session.start()
    try:
        feed_utterance(session)
        feed_utterance(session)
        assert done.wait(timeout=2.0)
    finally:
        session.stop()
    assert transcripts == ["second try"]

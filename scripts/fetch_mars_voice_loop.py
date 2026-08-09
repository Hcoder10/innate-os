#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Bake the ``mars_voice`` acceleration loop from the robot's own TTS voice.

The ``mars_voice`` costume in ``accel_voices`` plays a seamless loop of MARS
saying "brrrrr" back at a rate that rises with speed. This fetches that clip
from Cartesia through the service proxy, cuts the steadiest stretch of the
trill out of it, and crossfades end into start so the runtime can wrap the
buffer with no click and no windowing.

Run it only to regenerate the asset -- the result is committed, so a robot
never needs the network (or a Cartesia bill) to make engine noises::

    python3 scripts/fetch_mars_voice_loop.py

``INNATE_SERVICE_KEY`` must be set (``.env`` is read automatically).
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "ros2_ws/src/cloud/clients/proxy-client"))
sys.path.insert(0, str(REPO / "ros2_ws/src/cloud/clients/auth-client"))

DEFAULT_VOICE = "9fdaae0b-f885-4813-b589-3c07cf9d5fea"
"""The robot's default TTS voice, matching ``TTSHandler.DEFAULT_VOICE_ID``."""
TRANSCRIPT = "Brrrrrrrrrrrrrrrrrrrrrrrr"
FETCH_RATE = 44100
ASSET_RATE = 22050
"""The loop is a low buzz played back below 2x, so nothing in it lives near
11 kHz -- half the sample rate keeps the committed asset small for free."""
OUT = REPO / "ros2_ws/src/mars_bot/mars_control/assets/mars_voice_brr.wav"

LOOP_SECONDS = 0.30
CROSSFADE_SECONDS = 0.045


def fetch(voice_id: str) -> np.ndarray:
    """The raw TTS clip as mono floats."""
    from dotenv import load_dotenv

    from innate_proxy import ProxyClient

    load_dotenv(REPO / ".env")
    client = ProxyClient()
    raw = b"".join(
        client.cartesia.tts.bytes_stream(
            model_id="sonic-2",
            transcript=TRANSCRIPT,
            voice={"mode": "id", "id": voice_id},
            output_format={"container": "wav", "encoding": "pcm_s16le", "sample_rate": FETCH_RATE},
        )
    )
    # Cartesia streams a WAV whose length header is a placeholder, so the data
    # chunk is sliced by hand rather than trusted to the wave module.
    start = raw.find(b"data")
    body = raw[start + 8 :] if start >= 0 else raw
    return np.frombuffer(body[: len(body) // 2 * 2], dtype=np.int16).astype(np.float64) / 32768.0


def steadiest(audio: np.ndarray, rate: int, seconds: float) -> np.ndarray:
    """The window whose loudness holds most nearly constant.

    A trill's onset and its decay both loop badly -- the ear hears the clip
    restarting. The flattest stretch in the middle is the part that can be
    made to sound continuous, so the window is scored on loudness divided by
    variation rather than on loudness alone.
    """
    width = int(seconds * rate)
    step = max(width // 40, 1)
    envelope = np.abs(audio)
    best_score, best_start = -np.inf, 0
    for start in range(0, max(len(audio) - width, 1), step):
        window = envelope[start : start + width]
        score = float(window.mean()) / (float(window.std()) + 1e-6)
        if score > best_score:
            best_score, best_start = score, start
    return audio[best_start : best_start + width]


def seamless(loop: np.ndarray, rate: int, crossfade: float) -> np.ndarray:
    """Blend the loop's tail into its head so wrapping is inaudible.

    Costs the crossfade's worth of length: the returned loop is shorter than
    the input by exactly that much, because the tail is folded into the head
    rather than played after it.
    """
    fade = int(crossfade * rate)
    if fade * 2 >= len(loop):
        return loop
    body = loop[:-fade].copy()
    ramp = np.linspace(0.0, 1.0, fade)
    body[:fade] = body[:fade] * ramp + loop[-fade:] * (1.0 - ramp)
    return body


def resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio
    source = np.arange(len(audio)) / source_rate
    target = np.arange(int(len(audio) * target_rate / source_rate)) / target_rate
    return np.interp(target, source, audio)


def save(path: Path, audio: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-id", default=DEFAULT_VOICE, help="Cartesia voice to record")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    clip = fetch(args.voice_id)
    print(f"fetched {len(clip) / FETCH_RATE:.2f}s from Cartesia")

    window = steadiest(clip, FETCH_RATE, LOOP_SECONDS)
    loop = seamless(window, FETCH_RATE, CROSSFADE_SECONDS)
    loop = resample(loop, FETCH_RATE, ASSET_RATE)
    peak = float(np.abs(loop).max())
    if peak > 1e-6:
        loop = loop / peak * 0.95

    save(args.out, loop, ASSET_RATE)
    seam = float(np.abs(loop[0] - loop[-1]))
    print(f"wrote {args.out} — {len(loop) / ASSET_RATE:.3f}s loop, seam step {seam:.4f}")


if __name__ == "__main__":
    main()

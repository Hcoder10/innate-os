#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Bake the sampled acceleration voices from ElevenLabs sound effects.

Some sounds are not worth synthesising. A galloping hoof, a duck, a humpback
whale -- these carry so much irregular detail that an additive model of them
reads as a cartoon of the thing rather than the thing, whereas a recording of
one costs a few kilobytes and is simply correct.

What the generator gives back is a clip, though, and a clip does not
accelerate. So each one is cut down to a part that can be driven by road speed:

* ``loop``    the steadiest sustained stretch, trimmed to whole pitch periods
              where the material is pitched and crossfaded end-to-start, so
              ``accel_voices`` can wrap it and play it faster as speed rises.
* ``trigger`` one isolated hit, cut from just before its onset and faded out,
              for the voices that fire it at a rate set by speed.

Run it only to regenerate the assets. They are not committed -- they live in
``gs://karmanyaah-public/sounds/motor_voices/`` and ``post_update.sh`` (step
11c) fetches them, so publishing is the second half of regenerating::

    export ELEVENLABS_API_KEY=sk_...
    python3 scripts/fetch_sound_assets.py            # all of them
    python3 scripts/fetch_sound_assets.py gallop f1  # just these
    gsutil cp config/sounds/motor_voices/*.wav gs://karmanyaah-public/sounds/motor_voices/

A robot verifies each clip against the sha256 pinned in ``post_update.sh``, so
a *changed* clip is published by updating that line -- ``sha256sum`` on the new
file -- and every robot re-downloads it on the next update.

The key is read from the environment and never written to disk.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_mars_voice_loop import pitch, resample, save, seamless, whole_periods  # noqa: E402 — needs the path above

ENDPOINT = "https://api.elevenlabs.io/v1/sound-generation"
FETCH_RATE = 44100
ASSET_RATE = 22050
OUT = REPO / "config/sounds/motor_voices"

PITCHED = 0.45
"""Above this periodicity a loop is trimmed to whole pitch periods. Below it
the material is noise-like, where phase alignment means nothing and the
crossfade alone is enough."""


@dataclass(frozen=True)
class Recipe:
    """One sound to fetch and how to cut it down."""

    name: str
    prompt: str
    kind: str
    """``loop`` for something sustained to be wrapped and revved, ``trigger``
    for a single hit to be fired at a rate."""
    seconds: float
    """How much to ask the generator for."""
    take: float
    """Length of the loop, or of the extracted hit."""


RECIPES = (
    Recipe("gallop", "a single horse hoof striking hard dry dirt, close, dry, no reverb", "trigger", 3.0, 0.34),
    Recipe("quack", "a single duck quack, close, dry, no reverb", "trigger", 3.0, 0.40),
    Recipe("boing", "cartoon spring boing sound effect, single hit, dry", "trigger", 3.0, 0.45),
    Recipe("didgeridoo", "continuous didgeridoo drone, steady circular breathing, dry, no reverb", "loop", 9.0, 0.55),
    Recipe("whale", "humpback whale song, a long low tonal moan underwater", "loop", 9.0, 0.70),
    Recipe("f1", "formula one race car engine holding steady high revs, onboard", "loop", 9.0, 0.45),
    Recipe("crowd", "a large stadium crowd cheering continuously", "loop", 9.0, 1.10),
)


def generate(recipe: Recipe, key: str) -> np.ndarray:
    """The raw clip as mono floats."""
    import httpx

    response = httpx.post(
        ENDPOINT,
        params={"output_format": f"pcm_{FETCH_RATE}"},
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={"text": recipe.prompt, "duration_seconds": recipe.seconds, "prompt_influence": 0.6},
        timeout=180.0,
    )
    response.raise_for_status()
    # pcm_44100 comes back stereo-interleaved, not mono -- read it as mono and
    # every asset plays at half rate with the channels combed together.
    interleaved = np.frombuffer(response.content, dtype="<i2").astype(np.float64) / 32768.0
    return interleaved.reshape(-1, 2).mean(axis=1)


def steadiest(audio: np.ndarray, rate: int, seconds: float) -> np.ndarray:
    """The stretch whose loudness holds most nearly constant.

    Scored on mean over deviation, so a window is preferred for being *even*
    rather than loud -- the loudest part of a generated clip is usually its
    attack, which is the one stretch that cannot loop.
    """
    width = int(seconds * rate)
    step = max(width // 60, 1)
    envelope = np.abs(audio)
    best = (-np.inf, 0)
    for start in range(0, max(len(audio) - width, 1), step):
        window = envelope[start : start + width]
        score = float(window.mean()) / (float(window.std()) + 1e-6)
        best = max(best, (score, start))
    start = best[1]
    return audio[start : start + width]


def isolate(audio: np.ndarray, rate: int, seconds: float) -> np.ndarray:
    """One hit, cut from just before its onset, eased in and faded out.

    The lead-in matters twice over. Start exactly on the peak and the attack is
    already over, which is what turns a hoof into a thud; and start on a sample
    that is not near zero and every trigger fires with a click of its own on
    top, which is the loudest thing in the voice.
    """
    length = int(seconds * rate)
    lead = int(0.006 * rate)
    onset = max(int(np.argmax(np.abs(audio))) - lead, 0)
    hit = audio[onset : onset + length].copy()
    attack = min(int(0.002 * rate), len(hit) // 8)
    if attack:
        hit[:attack] *= np.linspace(0.0, 1.0, attack)
    fade = int(len(hit) * 0.25)
    if fade:
        hit[-fade:] *= np.linspace(1.0, 0.0, fade)
    return hit


def bake(recipe: Recipe, clip: np.ndarray) -> np.ndarray:
    if recipe.kind == "trigger":
        return isolate(clip, FETCH_RATE, recipe.take)
    window = steadiest(clip, FETCH_RATE, recipe.take)
    periodicity, f0 = pitch(window[: int(0.04 * FETCH_RATE)], FETCH_RATE)
    if periodicity > PITCHED:
        window = whole_periods(window, FETCH_RATE, f0)
    return seamless(window, FETCH_RATE, 0.03)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch the sampled acceleration voices")
    parser.add_argument("only", nargs="*", help="recipe names to regenerate (default: all)")
    args = parser.parse_args()

    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        raise SystemExit("ELEVENLABS_API_KEY is not set")

    for recipe in RECIPES:
        if args.only and recipe.name not in args.only:
            continue
        clip = generate(recipe, key)
        baked = resample(bake(recipe, clip), FETCH_RATE, ASSET_RATE)
        peak = float(np.abs(baked).max())
        if peak > 1e-6:
            baked = baked / peak * 0.95
        path = OUT / f"{recipe.name}.wav"
        save(path, baked, ASSET_RATE)
        print(f"{recipe.name:11s} {recipe.kind:7s} {len(baked) / ASSET_RATE:.3f}s  {path.stat().st_size // 1024:3d} KB")


if __name__ == "__main__":
    main()

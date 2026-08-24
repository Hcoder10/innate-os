#!/usr/bin/env python3
"""Offline checks on the backends, including the one that needs a key.

WHY THE GEMINI CHECK EXISTS. GeminiBackend was rewritten to read
Observation.image_path and then never run, because running it needs a key. An
untested request-builder is an untested claim, and the moment a key does arrive
is the worst time to discover the image never made it into the payload. The
network call is stubbed; everything up to it is real.

  ./sim/.venv/bin/python sim/bench/test_backends.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))

import backends as B  # noqa: E402
from brain_agent import ACTIONS, Observation  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    RESULTS.append((bool(ok), label))


def main() -> int:
    # --- the registry, and that the control still exists -------------------
    check("codex-blind" in B.BACKENDS, "a blind control backend is registered")
    check(B.BACKENDS["codex"].wants_image is True, "codex sees")
    check(B.BACKENDS["codex-blind"].wants_image is False, "codex-blind does not")
    check(
        B.BACKENDS["codex-blind"].__bases__[0] is B.CodexBackend,
        "the control is the SAME model as the seeing one, minus the camera",
    )

    # --- gemini refuses to run blind under a vision label -------------------
    saved = os.environ.pop("GEMINI_API_KEY", None)
    try:
        B.GeminiBackend()
        check(False, "gemini refuses to construct without a key")
    except RuntimeError:
        check(True, "gemini refuses to construct without a key")
    finally:
        if saved is not None:
            os.environ["GEMINI_API_KEY"] = saved

    # --- gemini actually puts the frame in the request ----------------------
    frame = BENCH / "results" / "_test_frame.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    # A tiny but real JPEG: two bytes of SOI plus filler is enough, since
    # nothing here decodes it -- what is under test is that the BYTES travel.
    frame.write_bytes(b"\xff\xd8\xff\xe0" + b"benchmark-test-frame" * 4)

    os.environ["GEMINI_API_KEY"] = "test-key-not-real"
    captured: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": '{"action":"forward","args":"{\\"metres\\":0.5}"}'}]}}]}
            ).encode()

    import urllib.request

    real_urlopen = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return FakeResponse()

    urllib.request.urlopen = fake_urlopen
    try:
        gem = B.GeminiBackend()
        obs = Observation(brief="test", elapsed_s=1.0, turns_left=5, image_path=str(frame))
        decision = gem.decide(obs, ACTIONS)
    finally:
        urllib.request.urlopen = real_urlopen
        os.environ.pop("GEMINI_API_KEY", None)
        frame.unlink(missing_ok=True)

    parts = captured.get("body", {}).get("contents", [{}])[0].get("parts", [])
    inline = next((p["inline_data"] for p in parts if "inline_data" in p), None)
    check(inline is not None, "the frame is attached to the request")
    if inline:
        want = base64.b64encode(b"\xff\xd8\xff\xe0" + b"benchmark-test-frame" * 4).decode()
        check(inline["data"] == want, "the attached bytes are the frame's own")
        check(inline["mime_type"] == "image/jpeg", "declared as jpeg")
    check(decision == {"action": "forward", "args": {"metres": 0.5}}, "the reply is parsed into an action")
    check("test-key-not-real" in captured.get("url", ""), "the key goes on the URL")

    # --- an observation with no frame must not fabricate one ----------------
    os.environ["GEMINI_API_KEY"] = "test-key-not-real"
    urllib.request.urlopen = fake_urlopen
    try:
        gem = B.GeminiBackend()
        gem.decide(Observation(brief="t", elapsed_s=0.0, turns_left=1, image_path=None), ACTIONS)
    finally:
        urllib.request.urlopen = real_urlopen
        os.environ.pop("GEMINI_API_KEY", None)
    parts = captured["body"]["contents"][0]["parts"]
    check(all("inline_data" not in p for p in parts), "no frame means no image part, not a blank one")

    for ok, label in RESULTS:
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    bad = sum(1 for ok, _ in RESULTS if not ok)
    print(f"\n{'all backend checks pass' if not bad else f'{bad} failed'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

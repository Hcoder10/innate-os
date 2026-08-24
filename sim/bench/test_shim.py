#!/usr/bin/env python3
"""Check the shim serves BOTH seams that GEMINI_BASE_URL steers.

That variable is read in two places, and a shim that satisfies one while
breaking the other is worse than no shim at all:

  innate/gemini.py       skill vision   OpenAI-compatible /v1/chat/completions
  brain/transport.py:35  every turn     native /v1beta/models/...:streamGenerateContent?alt=sse

The request shapes below are copied from those two callers, so this checks the
wire contract the real code will present rather than a convenient
approximation. The SSE case additionally checks that chunks ARRIVE
INCREMENTALLY: a shim that buffers the whole stream and returns it at the end
still passes a "did it work" test while changing the turn loop's timing.

Costs three small model calls with GEMINI_API_KEY set (vision, shim SSE, and the
direct control), two without. Run with the shim already listening.

  usage: test_shim.py [base_url]      (default http://127.0.0.1:8099)
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
GEMLIB_MODEL = "gemini-3.5-flash"  # innate/gemini.py MODEL
BRAIN_MODEL = "gemini-3.6-flash"  # brain_client's default
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok   ' if ok else 'FAIL '} {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def probe_jpeg() -> str:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (128, 96), (235, 235, 230))
    ImageDraw.Draw(image).ellipse([40, 30, 88, 70], fill=(200, 40, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return base64.b64encode(buffer.getvalue()).decode()


def test_openai_vision() -> None:
    """Exactly the body innate/gemini.ask_image builds."""
    encoded = probe_jpeg()
    body = {
        "model": GEMLIB_MODEL,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "What colour is the shape? Answer with one word."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
        ]}],
    }
    request = urllib.request.Request(
        BASE + "/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        data = json.loads(response.read())
        status = response.status

    check("skill vision: HTTP 200", status == 200, f"got {status}")
    # ask_image reads exactly this path and would KeyError on anything else.
    content = data.get("choices", [{}])[0].get("message", {}).get("content")
    check("skill vision: choices[0].message.content present", isinstance(content, str) and bool(content),
          repr(content)[:60])
    check("skill vision: it actually saw the image", isinstance(content, str) and "red" in content.lower(),
          repr(content)[:60])


def _stream_sse(base: str, headers: dict) -> tuple[int, int, str, float, float]:
    """(status, chunks, text, time to first chunk, time to last)."""
    body = {"contents": [{"role": "user", "parts": [{"text": "Count 1 to 40, one number per line."}]}]}
    path = f"/v1beta/models/{BRAIN_MODEL}:streamGenerateContent?alt=sse"
    request = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", **headers}, method="POST")

    started = time.time()
    arrivals: list[float] = []
    text = ""
    with urllib.request.urlopen(request, timeout=120) as response:
        status = response.status
        for raw in response:  # transport.py iterates lines and takes "data: "
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data: "):
                continue
            arrivals.append(time.time() - started)
            payload = json.loads(line[len("data: "):])
            for candidate in payload.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    text += part.get("text", "")
    return status, len(arrivals), text, (arrivals[0] if arrivals else 0.0), (arrivals[-1] if arrivals else 0.0)


def test_native_sse() -> None:
    """Exactly the request brain/transport.py streams, read the same way.

    Buffering is judged WITHIN the call, by the spread between the first chunk
    and the last. A shim that accumulates the whole response and returns it at
    once delivers every chunk at the same instant, whatever the upstream did,
    so a spread near zero is the signature and no control is needed.

    Two earlier versions of this check were wrong in opposite directions. The
    first asserted an absolute inter-chunk gap, which tests how Google paces
    its events rather than what the shim does. The second compared
    time-to-first-chunk against a separate direct call -- but those are two
    independent generations, so a slow one through the shim next to a fast one
    direct reads as buffering when nothing is wrong. It reported exactly that
    once (4.65s vs 1.96s) and the shim was fine. The direct call is still made,
    but only as printed context.

    The prompt is deliberately long enough (40 lines) that the upstream is
    certain to stream it over multiple segments; a one-word answer would arrive
    in a single chunk and could not distinguish the two cases at all.
    """
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    status, chunks, text, first, last = _stream_sse(BASE, {})

    check("brain stream: HTTP 200", status == 200, f"got {status}")
    check("brain stream: SSE chunks parsed", chunks > 0, f"{chunks} chunks")
    check("brain stream: text reassembled", "40" in text, repr(text[-24:]))

    spread = last - first
    check("brain stream: relayed, not buffered", chunks > 1 and spread > 0.05,
          f"{chunks} chunks spread over {spread:.2f}s "
          f"(first {first:.2f}s, last {last:.2f}s)")

    if key:
        # The control call is diagnostic only, so it gets its own try. Left
        # bare, a hiccup on Google's side during this EXTRA call raises through
        # main()'s handler and is recorded as "brain stream: raised" -- a shim
        # failure reported when the shim was never involved.
        try:
            _, direct_chunks, _, direct_first, direct_last = _stream_sse(
                "https://generativelanguage.googleapis.com", {"x-goog-api-key": key})
            print(f"        context: direct to Google was {direct_chunks} chunks over "
                  f"{direct_last - direct_first:.2f}s (first {direct_first:.2f}s); "
                  f"shim adds {first - direct_first:+.2f}s to first byte")
        except Exception as error:  # noqa: BLE001
            print(f"        context: direct control call failed ({type(error).__name__}); "
                  f"not a shim result")


def test_error_passthrough() -> None:
    """A real status must survive: callers branch on 404 vs 403 vs 500."""
    request = urllib.request.Request(BASE + "/v1beta/models/definitely-not-a-model:generateContent",
                                     data=b"{}", headers={"content-type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    check("upstream error status passes through", status in (400, 404),
          f"got {status} (not a shim-invented 502)")


def main() -> int:
    print(f"gemini shim at {BASE}:")
    for name, fn in (("skill vision", test_openai_vision),
                     ("brain stream", test_native_sse),
                     ("errors", test_error_passthrough)):
        try:
            fn()
        except Exception as error:  # noqa: BLE001
            check(f"{name}: raised", False, f"{type(error).__name__}: {error}")
    print(f"\n{'FAILED' if FAILURES else 'all pass'}"
          f" ({len(FAILURES)} failure{'s' if len(FAILURES) != 1 else ''})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

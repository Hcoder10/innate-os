#!/usr/bin/env python3
"""Measure what one brain turn actually costs, instead of guessing.

Wraps the same direct-Gemini path brain_client uses and reports Google's own
usageMetadata: prompt tokens, output tokens, and how much of the prompt was
cached. Those are the only numbers a budget estimate can honestly be built on
-- a robot brain's prompt is dominated by the skill roster and a camera frame,
neither of which is guessable from the outside.

Sends a request shaped like the brain's: a system instruction, a roster of
skills, and one camera-sized JPEG.
"""

import base64
import json
import os
import sys
import time
import urllib.request

MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.6-flash"
KEY = os.environ.get("GEMINI_API_KEY", "").strip()
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"


def jpeg_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def main() -> int:
    if not KEY:
        print("GEMINI_API_KEY not set in this environment")
        return 1

    frame = sys.argv[1] if len(sys.argv) > 1 else None
    roster = sys.argv[2] if len(sys.argv) > 2 else None

    parts = []
    if frame and os.path.exists(frame):
        blob = base64.b64encode(jpeg_bytes(frame)).decode()
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": blob}})
        print(f"frame: {frame} ({len(jpeg_bytes(frame))/1024:.1f} KB)")

    prompt = "You are a small mobile robot. Look at the image and say, in one sentence, what you see."
    if roster and os.path.exists(roster):
        text = open(roster, encoding="utf-8", errors="replace").read()
        prompt = text[:60000] + "\n\n" + prompt
        print(f"roster: {len(text)/1024:.1f} KB of skill descriptions prepended")
    parts.append({"text": prompt})

    body = json.dumps({"contents": [{"role": "user", "parts": parts}]}).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"x-goog-api-key": KEY, "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    dt = time.time() - t0

    u = data.get("usageMetadata", {})
    print(f"\nmodel   : {MODEL}")
    print(f"latency : {dt:.1f}s")
    print(f"prompt  : {u.get('promptTokenCount', 0):>7,} tokens")
    print(f"cached  : {u.get('cachedContentTokenCount', 0):>7,} tokens")
    print(f"output  : {u.get('candidatesTokenCount', 0):>7,} tokens")
    print(f"thoughts: {u.get('thoughtsTokenCount', 0):>7,} tokens")
    print(f"TOTAL   : {u.get('totalTokenCount', 0):>7,} tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())

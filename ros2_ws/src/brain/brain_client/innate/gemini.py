#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini vision via Innate proxy (/v1/chat/completions). Service key needs
"gemini" access or the proxy returns 403. Import as ``from innate import gemini``.
"""

import json
import time

from innate_proxy import ProxyClient

SERVICE = "gemini"
ENDPOINT = "/v1/chat/completions"
MODEL = "gemini-3.5-flash"


def make_client():
    """ProxyClient, or None if credentials missing."""
    client = ProxyClient()
    return client if client.is_available() else None


def ask_image(client, images_b64, question, logger=None, retries=3, cancelled=None):
    """JPEG(s) + question -> reply text. None if no client / all retries fail.
    images_b64: one base64 string or a list of them — sent in order, so the
    question can refer to them as image 1, image 2, ... Robot frames are
    640x480 JPEGs, ~100 KB each (~133 KB base64), at most two per call, so
    they go inline as data URLs; a file-upload API would only add a round
    trip and upload lifecycle for frames used once and discarded.
    cancelled: optional predicate checked before each attempt and through the
    backoff — when it turns true the call gives up (returns None) instead of
    riding out up to retries x proxy-timeout with a Stop pending."""
    if client is None:
        return None
    if isinstance(images_b64, str):
        images_b64 = [images_b64]
    content = [{"type": "text", "text": question}]
    content += [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images_b64]
    body = {
        "model": MODEL,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": content}],
    }
    for attempt in range(retries):
        if cancelled is not None and cancelled():
            return None
        try:
            with client.request_stream(
                SERVICE,
                ENDPOINT,
                method="POST",
                json=body,
            ) as resp:
                resp.raise_for_status()
                data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"] or ""
        except Exception as e:  # noqa: BLE001
            if logger:
                logger.warning(f"[gemini] vision call failed (try {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                deadline = time.time() + 2.0 * (attempt + 1)
                while time.time() < deadline:
                    if cancelled is not None and cancelled():
                        return None
                    time.sleep(0.1)
    return None

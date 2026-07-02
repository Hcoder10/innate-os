# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit test for TTS speech queueing: back-to-back speak_text_async() calls
(e.g. a skill calling say() twice in a row) must all play, in order — the old
behavior dropped any request that arrived while audio was playing."""

import logging
import time

from brain_client.transport.tts import TTSHandler


class _Proxy:
    """Just enough ProxyClient surface for TTSHandler to construct."""

    config: dict = {}
    cartesia = object()  # truthy -> is_available()

    def get_sync_client(self):
        raise RuntimeError("no network in tests")  # warmup thread swallows this

    proxy_url = "http://localhost"


def test_async_speech_queues_in_order_instead_of_dropping():
    handler = TTSHandler(logger=logging.getLogger("test"), proxy=_Proxy())

    spoken = []

    def fake_speak(text, voice_config=None):
        spoken.append(text)
        time.sleep(0.05)  # simulate playback so requests genuinely overlap
        return True

    handler.speak_text = fake_speak

    handler.speak_text_async("one")
    handler.speak_text_async("two")
    handler.speak_text_async("three")

    deadline = time.time() + 5
    while len(spoken) < 3 and time.time() < deadline:
        time.sleep(0.02)

    assert spoken == ["one", "two", "three"]
    handler.close()

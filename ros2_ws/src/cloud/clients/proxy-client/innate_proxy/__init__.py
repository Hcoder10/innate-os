# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Innate proxy client library.

Provides :class:`ProxyClient` for authenticated HTTP requests through the
Innate service proxy, plus drop-in adapter classes for Cartesia TTS and
ElevenLabs (Scribe realtime STT).

Usage::

    from innate_proxy import ProxyClient, ProxyCartesiaClient
"""

from innate_proxy.adapters.cartesia import ProxyCartesiaClient
from innate_proxy.adapters.elevenlabs import ProxyElevenLabsClient
from innate_proxy.client import ProxyClient
from innate_proxy.ws import SyncRealtimeConnection

__all__: list[str] = [
    "ProxyClient",
    "ProxyCartesiaClient",
    "ProxyElevenLabsClient",
    "SyncRealtimeConnection",
]

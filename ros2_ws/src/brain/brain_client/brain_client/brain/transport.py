# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""How the brain reaches Gemini: the Innate proxy (managed) or GEMINI_API_KEY (dev).

The proxy holds the upstream key and passes native ``:streamGenerateContent``
calls through untouched (the robot authenticates with its service key); the
direct path talks to ``generativelanguage.googleapis.com``. Both speak the same
wire format — a transport only moves chunks and never interprets them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING

import httpx

from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

PROXY_SERVICE = "gemini"
DIRECT_BASE_URL = "https://generativelanguage.googleapis.com"
STREAM_PATH = "/v1beta/models/{model}:streamGenerateContent?alt=sse"

Transport = Callable[[str, dict], Iterator[dict]]
"""(model, request body) -> streamed response chunks."""


class Backend(StrEnum):
    """Which way the brain reaches Gemini (surfaced in health and telemetry)."""

    PROXY = "innate-proxy"
    DIRECT = "gemini-direct"
    UNCONFIGURED = "unconfigured"


def pick_transport(proxy: ProxyClient | None) -> tuple[Transport | None, Backend]:
    """The way to reach Gemini: the Innate proxy (managed) or GEMINI_API_KEY (dev)."""
    if proxy is not None and proxy.is_available():
        return proxy_transport(proxy), Backend.PROXY
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        return direct_transport(api_key), Backend.DIRECT
    return None, Backend.UNCONFIGURED


def proxy_transport(proxy: ProxyClient) -> Transport:
    """Reach Gemini through the Innate proxy (the proxy holds the upstream key)."""

    def stream(model: str, body: dict) -> Iterator[dict]:
        endpoint = STREAM_PATH.format(model=model)
        with proxy.request_stream(PROXY_SERVICE, endpoint, json=body) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"gemini via proxy: HTTP {resp.status_code}: {resp.read()[:200]!r}")
            yield from _sse_chunks(resp.iter_lines())

    return stream


def direct_transport(api_key: str) -> Transport:
    """Reach Google's Gemini API directly with GEMINI_API_KEY."""
    # One client for the process: reuses the TLS connection across turns
    # instead of a fresh handshake per generate call. Single-threaded use by
    # construction (one turn at a time on the agent's worker thread).
    client = httpx.Client(headers={"x-goog-api-key": api_key}, timeout=90.0)

    def stream(model: str, body: dict) -> Iterator[dict]:
        url = DIRECT_BASE_URL + STREAM_PATH.format(model=model)
        with client.stream("POST", url, json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                raise RuntimeError(f"gemini direct: HTTP {resp.status_code}: {resp.text[:200]}")
            yield from _sse_chunks(resp.iter_lines())

    return stream


def _sse_chunks(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        if line.startswith("data: "):
            yield json.loads(line[len("data: ") :])

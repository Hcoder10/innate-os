# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure WebSocket URI/token validation — no ROS, no websockets.

Decides whether a websocket URI is usable and whether a service key is present
for the hosted Innate agent. Kept dependency-free so it is unit-testable in
isolation.
"""

from __future__ import annotations

_HOSTED_PREFIXES = ("wss://agent-v1.innate.bot", "wss://brain.innate.bot")


def validate_ws_uri(uri: str) -> bool:
    """True if the URI is a non-empty ws:// or wss:// URL."""
    if not uri or not uri.strip():
        return False
    return uri.startswith("ws://") or uri.startswith("wss://")


def is_hosted_innate_uri(uri: str) -> bool:
    """True if the URI points at the hosted Innate agent."""
    return uri.startswith(_HOSTED_PREFIXES)


def validate_token_for_uri(uri: str, token: str) -> bool:
    """A non-empty token is required only for the hosted agent."""
    if not is_hosted_innate_uri(uri):
        return True
    return bool((token or "").strip())

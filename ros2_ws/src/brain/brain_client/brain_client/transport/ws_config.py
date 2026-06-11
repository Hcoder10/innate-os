"""Pure WebSocket URI/token validation — no ROS, no websockets.

Decides whether a websocket URI is usable and whether a service key is acceptable
for the hosted Innate agent (rejecting obvious placeholders). Kept dependency-free
so it is unit-testable in isolation.
"""

from __future__ import annotations

PLACEHOLDER_SERVICE_KEYS = {
    "my_hardcoded_token",
    "your_service_key_here",
    "your-service-key-here",
    "replace_me",
    "changeme",
    "change_me",
    "todo",
}

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
    """A real (non-placeholder) token is required only for the hosted agent."""
    if not is_hosted_innate_uri(uri):
        return True
    normalized = (token or "").strip()
    if not normalized:
        return False
    return normalized.lower() not in PLACEHOLDER_SERVICE_KEYS

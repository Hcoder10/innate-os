"""Unit tests for pure WebSocket URI/token validation (no ROS required)."""

from brain_client.comms import ws_config


def test_validate_ws_uri():
    assert ws_config.validate_ws_uri("ws://localhost:8765")
    assert ws_config.validate_ws_uri("wss://brain.innate.bot")
    assert not ws_config.validate_ws_uri("")
    assert not ws_config.validate_ws_uri("   ")
    assert not ws_config.validate_ws_uri("http://example.com")


def test_is_hosted_innate_uri():
    assert ws_config.is_hosted_innate_uri("wss://agent-v1.innate.bot/x")
    assert ws_config.is_hosted_innate_uri("wss://brain.innate.bot")
    assert not ws_config.is_hosted_innate_uri("ws://localhost:8765")


def test_token_not_required_for_local():
    # Local/self-hosted URIs accept any token (even empty/placeholder).
    assert ws_config.validate_token_for_uri("ws://localhost:8765", "")
    assert ws_config.validate_token_for_uri("ws://localhost:8765", "changeme")


def test_token_required_and_non_placeholder_for_hosted():
    hosted = "wss://brain.innate.bot"
    assert not ws_config.validate_token_for_uri(hosted, "")
    assert not ws_config.validate_token_for_uri(hosted, "CHANGEME")  # case-insensitive placeholder
    assert not ws_config.validate_token_for_uri(hosted, "my_hardcoded_token")
    assert ws_config.validate_token_for_uri(hosted, "sk-real-key-123")

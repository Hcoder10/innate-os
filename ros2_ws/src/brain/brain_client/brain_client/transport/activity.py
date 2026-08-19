# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Small, dependency-free helpers for Agent activity events."""

from __future__ import annotations

import time
from typing import Any


def successful_code_output(result: Any, is_code: bool) -> str | None:
    """Return the exact user-facing output for a successful code skill."""
    message = getattr(result, "message", "")
    if (
        is_code
        and bool(getattr(result, "success", False))
        and getattr(result, "success_type", None) == "success"
        and isinstance(message, str)
        and message.strip()
    ):
        return message
    return None


def task_status_payload(
    primitive_name: str,
    primitive_id: str | None,
    status: str,
    *,
    skill_id: str | None = None,
    reason: str | None = None,
    args: dict | None = None,
    output: str | None = None,
    timestamp: float | None = None,
) -> dict:
    """Build the JSON object shared by task-status publishers."""
    payload = {
        "primitive_name": primitive_name,
        "primitive_id": primitive_id,
        "skill_name": primitive_name,
        "skill_id": skill_id or primitive_id,
        "status": status,
        "timestamp": time.time() if timestamp is None else timestamp,
    }
    if reason:
        payload["reason"] = reason
    if args:
        payload["args"] = args
    if output and output.strip():
        payload["output"] = output
    return payload


def task_status_history_entry(payload: Any) -> dict | None:
    """Convert a live skill-status payload into the chat-history shape."""
    if not isinstance(payload, dict):
        return None
    name = payload.get("primitive_name") or payload.get("skill_name") or payload.get("skill_id")
    status = payload.get("status")
    if not isinstance(name, str) or not name or not isinstance(status, str) or not status:
        return None

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        timestamp = time.time()
    entry = {
        "sender": "task_activated",
        "text": name,
        "timestamp": timestamp,
        "taskStatus": status,
    }
    primitive_id = payload.get("primitive_id")
    skill_id = payload.get("skill_id")
    if primitive_id is not None:
        entry["primitiveId"] = primitive_id
    if skill_id is not None:
        entry["skillId"] = skill_id
    if payload.get("args") is not None:
        entry["args"] = payload["args"]
    reason = payload.get("reason")
    if isinstance(reason, str) and reason:
        entry["failureReason"] = reason
    output = payload.get("output")
    if isinstance(output, str) and output.strip():
        entry["output"] = output
    return entry

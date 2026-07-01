# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared primitive lifecycle messages sent to the cloud agent."""

from __future__ import annotations

import json

from brain_client.transport.messages import MessageIn, MessageInType

PRIMITIVE_LIFECYCLE_MESSAGE_TYPES = {
    "running": MessageInType.PRIMITIVE_ACTIVATED,
    "completed": MessageInType.PRIMITIVE_COMPLETED,
    "interrupted": MessageInType.PRIMITIVE_INTERRUPTED,
    "failed": MessageInType.PRIMITIVE_FAILED,
}

# Children of a chaining skill run on the parent's goal, so they have no
# lifecycle of their own. We smuggle each child's start/finish out through the
# parent's feedback stream tagged with this sentinel; the runner pulls it back
# apart into a real per-step status (see PrimitiveRunner._on_feedback).
SUBSTEP_FEEDBACK_SENTINEL = "\x00substep\x00"


def encode_substep_feedback(
    *,
    event: str,
    name: str,
    primitive_id: str,
    skill_id: str,
    reason: str | None = None,
    output: str | None = None,
) -> str:
    """Pack a child step's lifecycle event into a tagged feedback string."""
    payload = {"event": event, "name": name, "primitive_id": primitive_id, "skill_id": skill_id}
    if reason:
        payload["reason"] = reason
    if output:
        payload["output"] = output
    return SUBSTEP_FEEDBACK_SENTINEL + json.dumps(payload)


def decode_substep_feedback(text: str) -> dict | None:
    """Unpack a substep marker, or None if this is just normal feedback."""
    if not text or not text.startswith(SUBSTEP_FEEDBACK_SENTINEL):
        return None
    try:
        return json.loads(text[len(SUBSTEP_FEEDBACK_SENTINEL) :])
    except (ValueError, TypeError):
        return None


def primitive_lifecycle_message(
    *,
    status: str,
    primitive_name: str,
    primitive_id: str | None,
    reason: str | None = None,
    output: str | None = None,
) -> MessageIn:
    payload = {"primitive_name": primitive_name, "primitive_id": primitive_id}
    if reason and status == "failed":
        payload["reason"] = reason
    if output and status == "completed":
        payload["output"] = output
    return MessageIn(type=PRIMITIVE_LIFECYCLE_MESSAGE_TYPES[status], payload=payload)

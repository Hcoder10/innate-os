"""Shared primitive lifecycle messages sent to the cloud agent."""

from __future__ import annotations

from brain_client.transport.messages import MessageIn, MessageInType

PRIMITIVE_LIFECYCLE_MESSAGE_TYPES = {
    "running": MessageInType.PRIMITIVE_ACTIVATED,
    "completed": MessageInType.PRIMITIVE_COMPLETED,
    "interrupted": MessageInType.PRIMITIVE_INTERRUPTED,
    "failed": MessageInType.PRIMITIVE_FAILED,
}


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

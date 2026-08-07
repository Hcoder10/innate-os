# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing recall over the robot's spatial memory.

A thin client of the brain's ``/brain/search_memory`` action — the Gemini
context cache, transport, and credentials all live server-side; a skill only
ever sees a typed :class:`RecallVerdict`. Declared like any interface::

    memory: SpatialMemory

    def execute(self, query: str):
        recall = self.memory.begin(query)
        verdict = self.wait_for(recall, timeout=60.0)

``begin`` returns a zero-arg reader that stays None until the verdict lands,
so the wait runs through ``self.wait_for`` — cancel-aware like every blocking
framework call. A skill that stops waiting simply abandons the goal; the
search concludes server-side and its verdict is dropped.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING

from brain_messages.action import SearchMemory
from rclpy.action import ActionClient

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class RecallVerdict:
    """One memory search's outcome. ``error`` non-empty means the search
    itself failed — distinct from a clean no-match (``found=False`` with an
    explanation). ``message`` is the model-ready sentence; the pose fields
    chain directly into navigation."""

    found: bool
    message: str
    explanation: str = ""
    error: str = ""
    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0
    seen_stamp: float = 0.0
    image: bytes | None = None
    latency_sec: float = 0.0
    cached: bool = False


class SpatialMemory:
    def __init__(self, node, logger):
        self._logger = logger
        self._client = ActionClient(node, SearchMemory, "/brain/search_memory")

    def begin(self, query: str) -> Callable[[], RecallVerdict | None]:
        """Start a search; hand the returned reader to ``self.wait_for``."""
        holder: list[RecallVerdict | None] = [None]

        def conclude(verdict: RecallVerdict) -> None:
            holder[0] = verdict

        if not self._client.wait_for_server(timeout_sec=2.0):
            conclude(_error_verdict("memory search unavailable — is the brain node running?"))
            return lambda: holder[0]

        def on_result(future) -> None:
            try:
                conclude(_from_result(future.result().result))
            except Exception as error:  # noqa: BLE001 — a lost result must still unblock the waiter
                conclude(_error_verdict(f"memory search result lost: {error}"))

        def on_goal(future) -> None:
            try:
                handle = future.result()
            except Exception as error:  # noqa: BLE001 — same: the waiter needs an answer, not a hang
                conclude(_error_verdict(f"memory search goal failed: {error}"))
                return
            if handle is None or not handle.accepted:
                conclude(_error_verdict("memory search goal rejected"))
                return
            handle.get_result_async().add_done_callback(on_result)

        goal = SearchMemory.Goal()
        goal.query = str(query)
        self._client.send_goal_async(goal).add_done_callback(on_goal)
        return lambda: holder[0]


def _error_verdict(error: str) -> RecallVerdict:
    return RecallVerdict(found=False, message=f"Memory search failed: {error}", error=error)


def _from_result(result) -> RecallVerdict:
    return RecallVerdict(
        found=result.found,
        message=result.message,
        explanation=result.explanation,
        error=result.error,
        x=result.x,
        y=result.y,
        theta=result.theta,
        seen_stamp=result.seen_stamp,
        image=base64.b64decode(result.image_b64) if result.image_b64 else None,
        latency_sec=result.latency_sec,
        cached=result.cached,
    )

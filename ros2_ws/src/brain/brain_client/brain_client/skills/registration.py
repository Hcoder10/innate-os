# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill catalog: track available skills and register them with the cloud agent.

Subscribes to the latched ``/brain/available_skills`` topic, rebuilds the shared
:class:`~brain_client.skills.registry.SkillRegistry`, and sends the registration
payload (skills filtered to the current directive + the directive prompt). Also
handles re-registration deferral while a primitive is running.
"""

from __future__ import annotations

import json
import time

from brain_messages.msg import AvailableSkills
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

from brain_client.skills.registry import SkillRegistry
from brain_client.transport.messages import MessageIn, MessageInType

AVAILABLE_SKILLS_QOS = QoSProfile(
    depth=1,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)

# Registering tells the cloud it may trigger skills, so hold the registration
# until the execute_skill action server is discoverable — otherwise an
# immediate first trigger races graph discovery and the dispatch fails
# (INN-711's root cause). Retry on a short timer; after the fail-open window
# register anyway so a wedged skills server degrades to per-trigger "failed"
# reports instead of a brain that never comes online.
REGISTRATION_GATE_RETRY_SEC = 0.5
REGISTRATION_GATE_FAIL_OPEN_SEC = 30.0


def registry_from_skills_msg(msg: AvailableSkills, on_duplicate=None) -> SkillRegistry:
    """Build a :class:`SkillRegistry` from an ``AvailableSkills`` message."""
    metadata = [
        {
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "guidelines": s.guidelines,
            "guidelines_when_running": s.guidelines_when_running,
            "inputs": json.loads(s.inputs_json) if s.inputs_json else {},
            "in_training": s.in_training,
            "episode_count": s.episode_count,
            "directory": s.directory,
            "wheeled": s.wheeled,
        }
        for s in msg.skills
    ]
    return SkillRegistry.from_metadata(metadata, on_duplicate=on_duplicate)


class SkillCatalog:
    def __init__(self, node, ws_bridge, state, *, execute_skill_ready=None):
        self._node = node
        self._logger = node.get_logger()
        self._ws = ws_bridge
        self._state = state

        # Callable returning whether the execute_skill action server is
        # discoverable (PrimitiveRunner.action_client.server_is_ready); None
        # disables the registration gate.
        self._execute_skill_ready = execute_skill_ready
        self._gate_timer = None
        self._gate_started: float | None = None

        self._last_skills_signature: tuple | None = None
        self._sub = node.create_subscription(
            AvailableSkills, "/brain/available_skills", self._on_available_skills, AVAILABLE_SKILLS_QOS
        )

    def _on_available_skills(self, msg: AvailableSkills) -> None:
        # The roster is latched and re-published on a heartbeat so late
        # subscribers (the webapp, via rws) can catch it. Ignore unchanged
        # repeats here so each beat doesn't rebuild the registry and re-hit the
        # cloud agent.
        signature = tuple(
            (
                s.id,
                s.name,
                s.type,
                s.guidelines,
                s.guidelines_when_running,
                s.inputs_json,
                s.in_training,
                s.episode_count,
                s.directory,
                s.wheeled,
            )
            for s in msg.skills
        )
        if signature == self._last_skills_signature:
            return
        self._last_skills_signature = signature

        def _warn_dup(name, existing_id, new_id):
            self._logger.warn(f"Duplicate skill name '{name}': ID '{existing_id}' overwritten by '{new_id}'")

        self._state.registry = registry_from_skills_msg(msg, on_duplicate=_warn_dup)

        counts = {t: sum(1 for s in msg.skills if s.type == t) for t in ("code", "learned", "replay")}
        self._logger.info(
            f"Received {len(msg.skills)} skills from topic: "
            f"{counts['code']} code, {counts['learned']} learned, {counts['replay']} replay"
        )

        # Re-register with the cloud agent if we were already registered.
        if self._state.primitives_registered:
            if self._state.primitive_running is None:
                self.register()
            else:
                self._logger.info("Deferring primitives re-registration — a skill is currently running")
                self._state.pending_reregistration = True

    def register(self) -> None:
        """Collect skill + directive definitions and send the registration message.

        Held back (with a retry timer) until the execute_skill action server is
        reachable: registration is the cloud's licence to trigger skills, and
        granting it before the server is discoverable makes the very first
        trigger fail (INN-711's root cause).
        """
        if self._state.current_directive is None:
            return
        if not self._gate_passed():
            return
        self._send_registration()

    def _gate_passed(self) -> bool:
        """True when registration may be sent now; otherwise schedules a retry."""
        if self._execute_skill_ready is None or self._execute_skill_ready():
            self._clear_gate()
            return True
        now = time.monotonic()
        if self._gate_started is None:
            self._gate_started = now
            self._logger.info("execute_skill server not ready; deferring registration until it is.")
        elif now - self._gate_started > REGISTRATION_GATE_FAIL_OPEN_SEC:
            self._logger.error(
                f"execute_skill server still not ready after {REGISTRATION_GATE_FAIL_OPEN_SEC:.0f}s — "
                "registering anyway (skill triggers will fail until it comes up)."
            )
            self._clear_gate()
            return True
        if self._gate_timer is None:
            self._gate_timer = self._node.create_timer(REGISTRATION_GATE_RETRY_SEC, self.register)
        return False

    def _clear_gate(self) -> None:
        self._gate_started = None
        if self._gate_timer is not None:
            self._node.destroy_timer(self._gate_timer)
            self._gate_timer = None

    def _send_registration(self) -> None:
        directive = self._state.current_directive
        primitives = self._state.registry.metadata
        active_skill_ids = set(self.active_skill_ids_for_registration())
        included = [p for p in primitives if p["id"] in active_skill_ids]

        reg_msg = MessageIn(
            type=MessageInType.REGISTER_PRIMITIVES_AND_DIRECTIVE,
            payload={
                "primitives": included,
                "directive": directive.get_prompt() or "",
                "token": self._state.token,
            },
        )
        self._logger.info(
            f"Registering {len(included)}/{len(primitives)} primitives and directive '{directive.id}' with server"
        )
        self._ws.send_message(reg_msg)

    def available_skill_ids(self) -> list[str]:
        return [p["id"] for p in self._state.registry.metadata]

    def active_skill_ids_for_registration(self) -> list[str]:
        if self._state.current_directive is None:
            return []
        current_skill_ids = (
            self._state.active_skill_ids
            if self._state.active_skill_ids is not None
            else list(self._state.current_directive.get_skills())
        )
        current_skill_set = set(current_skill_ids)
        return [skill_id for skill_id in self.available_skill_ids() if skill_id in current_skill_set]

    def set_active_skill_ids(self, requested_skills: list[str]) -> list[str]:
        available_skill_ids = self.available_skill_ids()
        requested_skill_set = set(requested_skills)
        self._state.active_skill_ids = [skill_id for skill_id in available_skill_ids if skill_id in requested_skill_set]
        return sorted(requested_skill_set - set(available_skill_ids))

    def drain_pending_reregistration(self) -> None:
        """Re-register if a re-registration was deferred during skill execution."""
        if self._state.pending_reregistration:
            self._state.pending_reregistration = False
            if self._state.primitives_registered:
                self._logger.info("Draining deferred primitives re-registration")
                self.register()

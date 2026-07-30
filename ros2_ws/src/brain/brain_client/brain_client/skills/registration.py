# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Track available skills (from /brain/available_skills) and register them
with the cloud agent, deferring re-registration while a primitive runs."""

from __future__ import annotations

import json

from brain_messages.msg import AvailableSkills
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

from brain_client.skills.registry import SkillRegistry
from brain_client.transport.messages import MessageIn, MessageInType

AVAILABLE_SKILLS_QOS = QoSProfile(
    depth=1,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)

# How often register() re-checks the execute_skill server while waiting for it.
REGISTRATION_RETRY_SEC = 0.5


def _metadata_from_msg(msg: AvailableSkills) -> list[dict]:
    # Skills with a load_error are on the roster for the UI only — never
    # registered with the cloud agent (they aren't runnable).
    return [
        {
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "group": s.group,
            "guidelines": s.guidelines,
            "guidelines_when_running": s.guidelines_when_running,
            "inputs": json.loads(s.inputs_json) if s.inputs_json else {},
            "in_training": s.in_training,
            "episode_count": s.episode_count,
            "directory": s.directory,
            "wheeled": s.wheeled,
        }
        for s in msg.skills
        if not s.load_error
    ]


def registry_from_skills_msg(msg: AvailableSkills, on_duplicate=None) -> SkillRegistry:
    """Build a :class:`SkillRegistry` from an ``AvailableSkills`` message."""
    return SkillRegistry.from_metadata(_metadata_from_msg(msg), on_duplicate=on_duplicate)


class SkillCatalog:
    def __init__(self, node, ws_bridge, state, *, execute_skill_ready=None):
        self._node = node
        self._logger = node.get_logger()
        self._ws = ws_bridge
        self._state = state

        # returns whether the execute_skill server is discoverable; None disables the wait
        self._execute_skill_ready = execute_skill_ready
        self._register_retry_timer = None

        self._last_skills_metadata: list | None = None
        self._sub = node.create_subscription(
            AvailableSkills, "/brain/available_skills", self._on_available_skills, AVAILABLE_SKILLS_QOS
        )

    def _on_available_skills(self, msg: AvailableSkills) -> None:
        # ignore unchanged heartbeat repeats so each beat doesn't rebuild the
        # registry and re-hit the cloud agent
        metadata = _metadata_from_msg(msg)
        if metadata == self._last_skills_metadata:
            return
        self._last_skills_metadata = metadata

        def _warn_dup(name, existing_id, new_id):
            self._logger.warn(f"Duplicate skill name '{name}': ID '{existing_id}' overwritten by '{new_id}'")

        self._state.registry = SkillRegistry.from_metadata(metadata, on_duplicate=_warn_dup)

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
        """Send the skills + directive registration to the cloud, waiting for
        the execute_skill server first — registering earlier lets the first
        trigger race graph discovery and fail (INN-711)."""
        if self._state.current_directive is None:
            self._stop_waiting_for_server()
            return
        if not self._server_ready():
            self._wait_for_server()
            return
        self._stop_waiting_for_server()
        self._send_registration()

    def _server_ready(self) -> bool:
        return self._execute_skill_ready is None or self._execute_skill_ready()

    def _wait_for_server(self) -> None:
        if self._register_retry_timer is None:
            self._logger.info("execute_skill server not ready; deferring registration until it is.")
            self._register_retry_timer = self._node.create_timer(REGISTRATION_RETRY_SEC, self.register)

    def _stop_waiting_for_server(self) -> None:
        if self._register_retry_timer is not None:
            self._node.destroy_timer(self._register_retry_timer)
            self._register_retry_timer = None

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
            else list(self._state.current_directive.skill_ids())
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

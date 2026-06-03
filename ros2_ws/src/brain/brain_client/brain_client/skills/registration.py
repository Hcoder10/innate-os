"""Skill catalog: track available skills and register them with the cloud agent.

Subscribes to the latched ``/brain/available_skills`` topic, rebuilds the shared
:class:`~brain_client.skills.registry.SkillRegistry`, and sends the registration
payload (skills filtered to the current directive + the directive prompt). Also
handles re-registration deferral while a primitive is running.
"""

from __future__ import annotations

import json

from brain_messages.msg import AvailableSkills
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

from brain_client.comms.messages import MessageIn, MessageInType
from brain_client.skills.registry import SkillRegistry


class SkillCatalog:
    def __init__(self, node, ws_bridge, state):
        self._node = node
        self._logger = node.get_logger()
        self._ws = ws_bridge
        self._state = state
        # Set by the node after the runner exists; True while a primitive runs.
        self.is_busy = lambda: False

        qos = QoSProfile(
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            reliability=QoSReliabilityPolicy.RELIABLE,
        )
        self._sub = node.create_subscription(AvailableSkills, "/brain/available_skills", self._on_available_skills, qos)

    def _on_available_skills(self, msg: AvailableSkills) -> None:
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
            }
            for s in msg.skills
        ]

        def _warn_dup(name, existing_id, new_id):
            self._logger.warn(f"Duplicate skill name '{name}': ID '{existing_id}' overwritten by '{new_id}'")

        self._state.registry = SkillRegistry.from_metadata(metadata, on_duplicate=_warn_dup)

        counts = {t: sum(1 for s in msg.skills if s.type == t) for t in ("code", "learned", "replay")}
        self._logger.info(
            f"Received {len(metadata)} skills from topic: "
            f"{counts['code']} code, {counts['learned']} learned, {counts['replay']} replay"
        )

        # Re-register with the cloud agent if we were already registered.
        if self._state.primitives_registered:
            if not self.is_busy():
                self.register()
            else:
                self._logger.info("Deferring primitives re-registration — a skill is currently running")
                self._state.pending_reregistration = True

    def register(self) -> None:
        """Collect skill + directive definitions and send the registration message."""
        if self._state.current_directive is None:
            return
        directive = self._state.current_directive
        primitives = self._state.registry.metadata
        included = [p for p in primitives if p["id"] in directive.get_skills()]

        reg_msg = MessageIn(
            type=MessageInType.REGISTER_PRIMITIVES_AND_DIRECTIVE,
            payload={
                "primitives": included if included else None,
                "directive": directive.get_prompt(),
                "token": self._state.token,
            },
        )
        self._logger.info(f"Registering {len(primitives)} primitives and directive '{directive.id}' with server")
        self._ws.send_message(reg_msg)

    def drain_pending_reregistration(self) -> None:
        """Re-register if a re-registration was deferred during skill execution."""
        if self._state.pending_reregistration:
            self._state.pending_reregistration = False
            if self._state.primitives_registered:
                self._logger.info("Draining deferred primitives re-registration")
                self.register()

"""Brain lifecycle: activate / deactivate / reset / reactivate + directive switching.

Owns the agent timer and the on-demand sensor subscriptions (delegated to the
camera and pose-tracker collaborators) and the memory-positions timer. This is the
state machine that turns the brain's perception loop on and off.
"""

from __future__ import annotations

import json

from std_msgs.msg import String

from brain_client.transport.messages import InternalMessage, InternalMessageType, MessageIn, MessageInType


class BrainLifecycle:
    def __init__(
        self,
        node,
        state,
        config,
        *,
        ws_bridge,
        camera,
        pose_tracker,
        runner,
        gaze,
        chat,
        orchestrator,
        catalog,
        active_inputs_pub,
        stop_robot,
    ):
        self._node = node
        self._logger = node.get_logger()
        self._state = state
        self._config = config
        self._ws = ws_bridge
        self._camera = camera
        self._pose = pose_tracker
        self._runner = runner
        self._gaze = gaze
        self._chat = chat
        self.orchestrator = orchestrator
        self.catalog = catalog
        self._active_inputs_pub = active_inputs_pub
        self._stop_robot = stop_robot

        self.agent_timer = None
        self._memory_timer = None
        self._reactivate_timer = None

    # --- timers / subscriptions ---
    def start_agent_timer(self) -> None:
        if self.agent_timer is None or self.agent_timer.is_canceled():
            self.agent_timer = self._node.create_timer(0.1, self.orchestrator.agent_loop)

    def start_brain_subscriptions(self) -> None:
        self._camera.start()
        self._pose.start()
        if self._memory_timer is None:
            self._memory_timer = self._node.create_timer(1.0, self.orchestrator.publish_memory_positions)
        self._logger.info("Brain subscriptions started")

    def stop_brain_subscriptions(self) -> None:
        self._camera.stop()
        self._pose.stop()
        if self._memory_timer is not None:
            self._memory_timer.cancel()
            self._memory_timer = None
        self._logger.info("Brain subscriptions stopped")

    # --- inputs / registration ---
    def activate_directive_inputs(self) -> None:
        directive = self._state.current_directive
        if not directive or not self._state.is_brain_active:
            return
        try:
            required_inputs = directive.get_inputs()
            self._active_inputs_pub.publish(String(data=json.dumps({"inputs": required_inputs})))
            if required_inputs:
                self._logger.info(f"🔌 Activated inputs for directive '{directive.id}': {required_inputs}")
            else:
                self._logger.debug(f"No inputs required for directive '{directive.id}'")
        except Exception as e:
            self._logger.error(f"Error activating directive inputs: {e}")

    def unregister_primitives(self) -> None:
        self._logger.info("[BrainClient] Unregistering primitives")
        self._state.primitives_registered = False
        self._runner.abort_running()

    # --- reset ---
    def perform_brain_reset(self, memory_state: str) -> None:
        self._logger.info("[BrainClient] Resetting brain")
        self.unregister_primitives()
        self._chat.clear()
        self._ws.send_message(MessageIn(type=MessageInType.RESET, payload={"memory_state": memory_state}))
        self._chat.emit_system(f"Brain has been reset with memory state: {memory_state}")
        self.catalog.register()

    # --- activate / deactivate ---
    def activate_for_simulator(self) -> None:
        self._state.is_brain_active = True
        self.start_brain_subscriptions()

    def deactivate_brain(self) -> None:
        self._logger.info("[BrainClient] Deactivating brain...")
        self._state.is_brain_active = False
        self.stop_brain_subscriptions()

        if self.agent_timer and not self.agent_timer.is_canceled():
            self.agent_timer.cancel()
            self._logger.info("Agent timer cancelled.")

        if self._reactivate_timer is not None and not self._reactivate_timer.is_canceled():
            self._reactivate_timer.cancel()

        self._state.ready_for_image = False
        self._state.primitives_registered = False

        self._runner.interrupt_for_deactivation()
        self._gaze.stop()

        self._active_inputs_pub.publish(String(data=json.dumps({"inputs": []})))
        self._logger.info("🔌 Deactivated all inputs")

        self._stop_robot()
        self._logger.info("[BrainClient] Brain deactivated.")

    def reactivate_brain(self) -> None:
        self._logger.info("[BrainClient] Reactivating brain...")
        if self._state.current_directive is None:
            self._logger.warn("[BrainClient] No directive configured. Brain remains inactive.")
            return

        self._state.is_brain_active = True
        self.start_brain_subscriptions()
        self._logger.info(f"[BrainClient] Continuing with current directive: {self._state.current_directive.id}")

        self._gaze.update()
        self.start_agent_timer()

        self._logger.info("[BrainClient] Sending READY_FOR_CONNECTION to server.")
        self._ws.send_message(InternalMessage(type=InternalMessageType.READY_FOR_CONNECTION))

        # Re-register after a short delay (lets the server process
        # READY_FOR_CONNECTION) via a one-shot timer, so we don't block the
        # executor thread with a sleep.
        if self._reactivate_timer is not None:
            self._node.destroy_timer(self._reactivate_timer)
        self._reactivate_timer = self._node.create_timer(0.5, self._finish_reactivation)

    def _finish_reactivation(self) -> None:
        self._reactivate_timer.cancel()  # one-shot
        if not self._state.is_brain_active:
            return  # deactivated during the wait; nothing to re-register
        self.unregister_primitives()
        self.catalog.register()
        self.activate_directive_inputs()
        self._state.ready_for_image = True
        self._logger.info("[BrainClient] Brain reactivated and skills re-registered.")

    # --- directive switching ---
    def set_directive(self, name: str) -> None:
        name = name.strip()
        self._logger.info(f"Received directive change request: {name}")
        if name not in self._state.directives:
            available = list(self._state.directives.keys())
            self._chat.emit_system(f"Error: Unknown directive '{name}'. Available directives: {available}")
            self._logger.error(f"Unknown directive: {name}")
            return
        self._state.current_directive = self._state.directives[name]
        self._logger.info(f"Activated directive: {name}")
        self._chat.clear()
        self._ws.send_message(MessageIn(type=MessageInType.RESET, payload={"memory_state": "clear"}))
        self.catalog.register()
        self.activate_directive_inputs()
        self._gaze.update()
        self._chat.emit_system(f"Directive updated to: {name}")

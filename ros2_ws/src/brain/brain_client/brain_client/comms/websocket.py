#!/usr/bin/env python3
"""In-process bridge between the brain and the WebSocket transport.

Outgoing: hands message *objects* straight to the
:class:`~brain_client.comms.ws_manager.WebSocketManager` (one serialization, no
ROS topic). ``send_message`` is called on the executor thread and the manager uses
``run_coroutine_threadsafe`` to reach the socket's asyncio loop, so it is safe.

Incoming: the transport's listener runs on the websocket asyncio thread and calls
:meth:`enqueue_incoming` — which only puts the raw string on a thread-safe queue and
triggers an rclpy guard condition. The executor thread then drains the queue and
runs the handlers via :meth:`dispatch`. This is the critical detail: handlers
(which touch primitive state, chat history, the action client, …) must never run on
the websocket thread, or they race the executor thread's timers/callbacks.
"""

from __future__ import annotations

import json
import queue

from brain_client.comms.messages import MessageOut, MessageOutType


def _payload_summary(data):
    payload = data.get("payload")
    if isinstance(payload, dict):
        keys = ", ".join(sorted(str(key) for key in payload.keys())[:8])
        return f" payload keys=[{keys}]"
    if payload is None:
        return " payload=<missing>"
    return f" payload_type={type(payload).__name__}"


class WSBridge:
    def __init__(self, node, transport):
        self.node = node
        self.handlers = {}  # MessageOutType -> handler callback
        self._transport = transport

        # Thread-safe hand-off: the websocket asyncio thread enqueues raw messages
        # and triggers the guard; the executor thread drains + dispatches them.
        self._incoming: queue.SimpleQueue = queue.SimpleQueue()
        self._incoming_guard = node.create_guard_condition(self._drain_incoming)
        node.get_logger().info("WSBridge: in-process transport with threadsafe incoming hand-off.")

    def register_handler(self, msg_type: MessageOutType, handler):
        """Register a (synchronous) handler for a given outgoing message type."""
        self.handlers[msg_type] = handler

    # --- incoming (websocket asyncio thread -> executor thread) ---
    def enqueue_incoming(self, raw: str):
        """Called on the websocket thread: queue the message and wake the executor."""
        self._incoming.put(raw)
        self._incoming_guard.trigger()

    def _drain_incoming(self):
        """Guard-condition callback, runs on the executor thread."""
        while True:
            try:
                raw = self._incoming.get_nowait()
            except queue.Empty:
                break
            self.dispatch(raw)

    def dispatch(self, raw: str):
        """Parse, validate, and dispatch one incoming JSON message (executor thread)."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self.node.get_logger().error(f"WSBridge: Failed to parse JSON: {e}")
            return

        msg_type_str = data.get("type")
        if msg_type_str is None:
            self.node.get_logger().error("WSBridge: Message missing 'type' field")
            return

        try:
            ws_msg = MessageOut.model_validate(data)
        except Exception as e:
            first_line = str(e).splitlines()[0]
            self.node.get_logger().error(
                "WSBridge: Incoming backend message is not supported "
                f"(type={msg_type_str!r},{_payload_summary(data)}): {first_line}"
            )
            return

        handler = self.handlers.get(ws_msg.type)
        if handler:
            try:
                handler(ws_msg)
            except Exception as e:
                self.node.get_logger().error(f"WSBridge: Handler for {ws_msg.type} raised error: {e}")
        else:
            self.node.get_logger().warn(f"WSBridge: No handler registered for message type: {ws_msg.type}")

    # --- outgoing (executor thread) ---
    def send_message(self, message):
        """Forward an outgoing message object (MessageIn / InternalMessage)."""
        self._transport.handle_outgoing(message)

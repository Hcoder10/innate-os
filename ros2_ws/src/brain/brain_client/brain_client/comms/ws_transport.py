"""The async WebSocket client transport.

Owns the connect/auth/listen/reconnect loop and forwards incoming messages back to
its node (which publishes them onto ROS). The node provides the small interface
this relies on: ``set_ws_status``, ``ws_pub``, ``exit_event``, ``ws_client``,
``_handle_ws_error`` and the last-error bookkeeping.
"""

from __future__ import annotations

import asyncio
import json
import time

import rclpy
import websockets
from std_msgs.msg import String

from brain_client.comms.messages import MessageIn, MessageInType

CONNECTED_GRACE_SECONDS = 2.0
WS_THREAD_STOP_JOIN_SECONDS = 0.25


class WSClient:
    def __init__(self, uri, token, node, stop_event, robot_version: str | None = None):
        """:param node: the ROS node, used for logging, publishing, and status."""
        self.uri = uri
        self.token = token
        self.node = node
        self.stop_event = stop_event
        self.robot_version = robot_version
        self.websocket = None
        self.loop = None

    def should_stop(self):
        return self.stop_event.is_set() or self.node.exit_event.is_set() or not rclpy.ok()

    def is_current_client(self):
        return getattr(self.node, "ws_client", None) is self

    def set_status(self, state: str, connected: bool, message: str):
        if self.is_current_client():
            self.node.set_ws_status(state, connected, message)

    async def close(self):
        if self.websocket is not None:
            await self.websocket.close(code=1000, reason="backend config updated")

    async def _sleep_before_reconnect(self, delay):
        deadline = time.time() + delay
        while not self.should_stop() and time.time() < deadline:
            await asyncio.sleep(min(0.2, max(0.0, deadline - time.time())))

    async def connect(self):
        """Connect to the WebSocket server with automatic reconnection."""
        reconnect_delay = 1
        max_reconnect_delay = 30

        while not self.should_stop():
            disconnect_reason = "Connection lost."
            status_reported = False
            self.node.get_logger().info(f"[WSClient] Connecting to {self.uri} ...")
            self.set_status("connecting", False, f"Connecting to {self.uri}")
            try:
                async with websockets.connect(
                    self.uri,
                    ping_interval=30,
                    ping_timeout=60,
                    open_timeout=5,
                ) as websocket:
                    self.websocket = websocket
                    self.set_status("authenticating", False, f"Socket opened to {self.uri}; authenticating.")
                    reconnect_delay = 1

                    auth_payload = {"token": self.token}
                    if self.robot_version:
                        auth_payload["client_version"] = self.robot_version
                    await self.send(MessageIn(type=MessageInType.AUTH, payload=auth_payload))
                    self.node.get_logger().debug(f"[WSClient] Auth message sent with version: {self.robot_version}")
                    self.set_status("authenticating", False, "Auth message sent; waiting for backend confirmation.")

                    disconnect_reason = await self.listen()
            except Exception as e:
                disconnect_reason = f"Connection error: {e}"
                self.set_status("connection_error", False, disconnect_reason)
                status_reported = True

            self.websocket = None
            if self.should_stop():
                break

            self.node.get_logger().warn(f"[WSClient] {disconnect_reason} Reconnecting in {reconnect_delay}s...")
            if not status_reported:
                self.set_status("disconnected", False, f"{disconnect_reason} Reconnecting in {reconnect_delay}s.")
            await self._sleep_before_reconnect(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

        self.node.get_logger().info("[WSClient] Stopping WSClient.")
        self.websocket = None
        self.set_status("stopped", False, "WebSocket client stopped.")

    async def listen(self):
        connected_started_at = time.time()
        connected_reported = False

        while not self.should_stop():
            try:
                incoming = await asyncio.wait_for(self.websocket.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                if not connected_reported and time.time() - connected_started_at >= CONNECTED_GRACE_SECONDS:
                    self.node.get_logger().info(f"[WSClient] Connected to {self.uri}")
                    self.set_status("connected", True, f"Connected to {self.uri}")
                    connected_reported = True
                continue
            except websockets.exceptions.ConnectionClosed as e:
                reason = e.reason or "no reason"
                message = f"WebSocket connection closed by server (code={e.code}, reason={reason})."
                if (
                    reason == "no reason"
                    and self.node._last_ws_error_message
                    and time.time() - self.node._last_ws_error_at < 60.0
                ):
                    message = f"{message} Last backend error: {self.node._last_ws_error_message}"
                return message

            try:
                data = json.loads(incoming)
                msg_type = data.get("type")
                normalized_type = msg_type.rsplit("/", 1)[-1] if isinstance(msg_type, str) else msg_type
                if normalized_type == "error":
                    payload = data.get("payload", {})
                    error_type = payload.get("error", "unknown")
                    message = payload.get("message", "Unknown error")
                    if self.is_current_client():
                        self.node._handle_ws_error(error_type, message)
            except json.JSONDecodeError:
                pass

            if not connected_reported and time.time() - connected_started_at >= CONNECTED_GRACE_SECONDS:
                self.node.get_logger().info(f"[WSClient] Connected to {self.uri}")
                self.set_status("connected", True, f"Connected to {self.uri}")
                connected_reported = True

            if not self.is_current_client():
                self.node.get_logger().debug("Dropping incoming message from stale WebSocket client.")
                continue
            self.node.get_logger().debug(f"Forwarding incoming message: {incoming}")
            self.node.ws_pub.publish(String(data=incoming))

        return "WebSocket listener stopped."

    async def send(self, msg):
        if self.websocket is not None:
            await self.websocket.send(msg.model_dump_json())

#!/usr/bin/env python3
"""WebSocket client node: bridges the cloud agent socket to ROS topics.

Thin-ish composition root. The async transport lives in
:mod:`brain_client.comms.ws_transport` (``WSClient``) and the pure URI/token
validation in :mod:`brain_client.comms.ws_config`; this node owns the ROS surface
(params, pubs/subs, status), the websocket thread lifecycle, and outgoing-message
routing.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from brain_client.comms.messages import InternalMessage, InternalMessageType, MessageIn, MessageInType
from brain_client.comms.ws_config import is_hosted_innate_uri, validate_token_for_uri, validate_ws_uri
from brain_client.comms.ws_transport import WS_THREAD_STOP_JOIN_SECONDS, WSClient


class WSClientNode(Node):
    def __init__(self):
        super().__init__("ws_client_node")

        self.declare_parameter("websocket_uri", "ws://localhost:8765")
        self.declare_parameter("token", "MY_HARDCODED_TOKEN")
        self.declare_parameter("client_version", "")
        self.ws_uri = self.get_parameter("websocket_uri").get_parameter_value().string_value
        self.token = self.get_parameter("token").get_parameter_value().string_value
        configured_client_version = (self.get_parameter("client_version").get_parameter_value().string_value).strip()
        self.exit_event = threading.Event()
        self._ws_stop_event = threading.Event()

        self._ws_status_pub = self.create_publisher(String, "/brain/websocket_status", 10)
        self._ws_status = {
            "state": "starting",
            "connected": False,
            "message": "WebSocket client starting.",
            "uri": self.ws_uri,
            "hosted": False,
            "timestamp": time.time(),
        }
        self._ws_status_timer = self.create_timer(2.0, self._publish_ws_status)
        self._last_backend_unavailable_log_key = None
        self._last_backend_unavailable_log_at = 0.0
        self._last_invalid_config_log_at = 0.0
        self._last_ws_error_message = ""
        self._last_ws_error_at = 0.0

        # Robot version from launch config first, then /robot/info when available.
        self._robot_version: str | None = configured_client_version or None
        self._robot_info_sub = self.create_subscription(String, "/robot/info", self._robot_info_callback, 10)
        if self._robot_version:
            self.get_logger().info(f"Robot version: {self._robot_version}")

        self._tts_pub = self.create_publisher(String, "/brain/tts", 10)
        self.ws_pub = self.create_publisher(String, "ws_messages", 10)
        self.outgoing_sub = self.create_subscription(String, "ws_outgoing", self.ws_outgoing_callback, 10)
        self.backend_config_sub = self.create_subscription(
            String, "/brain/backend_config", self.backend_config_callback, 10
        )

        self.ws_client = None
        self.ws_thread = None
        self._configure_ws_client(log_invalid=True)
        self.get_logger().info("WSClientNode initialized.")

    def _refresh_config_flags(self):
        self._ws_configured = validate_ws_uri(self.ws_uri)
        self._hosted_innate_uri = is_hosted_innate_uri(self.ws_uri)
        self._token_configured = validate_token_for_uri(self.ws_uri, self.token)

    def _configure_ws_client(self, log_invalid: bool = False, publish_configured_status: bool = True):
        self._refresh_config_flags()
        if not self._ws_configured:
            if log_invalid:
                self._log_invalid_config_once(
                    "❌ WebSocket URI not configured or invalid. Set 'websocket_uri' "
                    "parameter (must start with ws:// or wss://)."
                )
            self.ws_client = None
            self.set_ws_status("invalid_config", False, "WebSocket URI is not configured or invalid.")
            return

        if not self._token_configured:
            if log_invalid:
                self._log_invalid_config_once(
                    "❌ Missing or placeholder INNATE_SERVICE_KEY for hosted Innate agent. "
                    "Set a real service key before connecting to the hosted agent."
                )
            self.ws_client = None
            self.set_ws_status(
                "invalid_config",
                False,
                "Missing or placeholder INNATE_SERVICE_KEY for hosted Innate agent.",
            )
            return

        self.ws_client = WSClient(self.ws_uri, self.token, self, self._ws_stop_event, self._robot_version)
        if publish_configured_status:
            self.set_ws_status("configured", False, "WebSocket configured; waiting for connection request.")

    def _stop_ws_thread(self):
        old_client = self.ws_client
        old_thread = self.ws_thread
        self._ws_stop_event.set()
        if old_client and old_client.loop and old_client.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(old_client.close(), old_client.loop)
            except RuntimeError as e:
                self.get_logger().debug(f"Could not close websocket cleanly: {e}")
        if old_thread and old_thread.is_alive():
            old_thread.join(timeout=WS_THREAD_STOP_JOIN_SECONDS)
            if old_thread.is_alive():
                self.get_logger().warn(
                    "[WSClient] Old WebSocket thread did not stop within "
                    f"{WS_THREAD_STOP_JOIN_SECONDS:.2f}s; proceeding with reconnect."
                )
            else:
                self.get_logger().debug("[WSClient] Old WebSocket thread stopped.")
        self.ws_thread = None
        self._ws_stop_event = threading.Event()

    def _start_ws_thread(self):
        if self.ws_client is None:
            return
        if self.ws_thread is None or not self.ws_thread.is_alive():
            self.ws_thread = threading.Thread(target=self.run_ws_loop, args=(self.ws_client,))
            self.ws_thread.start()

    def backend_config_callback(self, msg: String):
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            self.get_logger().error("Invalid /brain/backend_config payload JSON.")
            return
        if not isinstance(payload, dict):
            self.get_logger().error("/brain/backend_config payload must be an object.")
            return

        new_uri = str(payload.get("websocket_uri") or self.ws_uri).strip()
        new_token = str(payload.get("service_key") or payload.get("token") or self.token).strip()
        if new_uri == self.ws_uri and new_token == self.token:
            self.get_logger().info("[WSClient] Backend config unchanged.")
            return

        self.get_logger().info(
            f"[WSClient] Applying backend config update (uri={new_uri}, "
            f"service_key={'provided' if new_token else 'empty'})."
        )
        self._stop_ws_thread()
        self.ws_uri = new_uri
        self.token = new_token
        self._last_ws_error_message = ""
        self._last_ws_error_at = 0.0
        self._configure_ws_client(log_invalid=True, publish_configured_status=False)
        if self.ws_client is not None:
            self.set_ws_status("connecting", False, "Backend config updated; reconnecting.")
            self._start_ws_thread()

    def set_ws_status(self, state: str, connected: bool, message: str):
        """Update and publish cloud/local-agent websocket status."""
        self._ws_status = {
            "state": state,
            "connected": connected,
            "message": message,
            "uri": self.ws_uri,
            "hosted": self._hosted_innate_uri,
            "timestamp": time.time(),
        }
        self._publish_ws_status()
        self._log_backend_unavailable_once(state, connected, message)

    def _log_backend_unavailable_once(self, state: str, connected: bool, message: str):
        if connected or state in {"starting", "configured", "connecting", "authenticating"}:
            return
        now = time.time()
        key = (state, message)
        if key == self._last_backend_unavailable_log_key and now - self._last_backend_unavailable_log_at < 30.0:
            return
        self._last_backend_unavailable_log_key = key
        self._last_backend_unavailable_log_at = now
        self.get_logger().error(f"[WSClient] Brain backend unavailable (state={state}, uri={self.ws_uri}): {message}")

    def _log_invalid_config_once(self, message: str):
        now = time.time()
        if now - self._last_invalid_config_log_at < 30.0:
            return
        self._last_invalid_config_log_at = now
        self.get_logger().error(message)

    def _publish_ws_status(self):
        try:
            self._ws_status_pub.publish(String(data=json.dumps(self._ws_status)))
        except Exception as e:
            self.get_logger().debug(f"Failed to publish websocket status: {e}")

    def _robot_info_callback(self, msg: String):
        """Extract robot version from /robot/info."""
        try:
            data = json.loads(msg.data)
            version = data.get("version")
            if version and version != self._robot_version:
                self._robot_version = version
                self.get_logger().info(f"Robot version: {version}")
                if self.ws_client:
                    self.ws_client.robot_version = version
        except Exception as e:
            self.get_logger().debug(f"Error parsing robot info: {e}")

    def _speak_error(self, message: str):
        try:
            self._tts_pub.publish(String(data=message))
            self.get_logger().info(f"Speaking error: {message}")
        except Exception as e:
            self.get_logger().error(f"Error publishing to TTS: {e}")

    def _handle_ws_error(self, error_type: str, message: str):
        """Handle WebSocket error messages forwarded by the transport."""
        self._last_ws_error_message = f"{error_type}: {message}"
        self._last_ws_error_at = time.time()
        if error_type == "version_mismatch":
            self._speak_error(message)
            self.get_logger().error(f"Version mismatch: {message}")
            self.set_ws_status("backend_error", False, f"Version mismatch: {message}")
        else:
            self.get_logger().error(f"WebSocket error: {error_type} - {message}")
            self.set_ws_status("backend_error", False, f"Backend error ({error_type}): {message}")

    def ws_outgoing_callback(self, msg: String):
        """Validate an outgoing message and forward it to the WebSocket server."""
        try:
            data = json.loads(msg.data)
            if "type" not in data:
                self.get_logger().error(f"Outgoing message missing 'type': {msg.data}")
                return
            message_type = data.get("type")

            if message_type in [t.value for t in InternalMessageType]:
                internal_message = InternalMessage.model_validate(data)
                self.get_logger().info(f"Internal message: {internal_message}")
                if internal_message.type == InternalMessageType.READY_FOR_CONNECTION:
                    if not self._ws_configured:
                        self._log_invalid_config_once("WebSocket not configured, ignoring connection request.")
                        self.set_ws_status("invalid_config", False, "WebSocket URI is not configured or invalid.")
                        return
                    if not self._token_configured:
                        self._log_invalid_config_once(
                            "Hosted Innate agent selected but INNATE_SERVICE_KEY is missing or a placeholder. "
                            "Skipping websocket connection."
                        )
                        self.set_ws_status(
                            "invalid_config",
                            False,
                            "Missing or placeholder INNATE_SERVICE_KEY for hosted Innate agent.",
                        )
                        return
                    self.get_logger().debug("Received ready for connection message.")
                    if self.ws_thread and self.ws_thread.is_alive():
                        self.get_logger().debug("WebSocket thread is already running.")
                    else:
                        self._start_ws_thread()
            elif message_type in [t.value for t in MessageInType]:
                outgoing_message = MessageIn.model_validate(data)
                self.get_logger().debug(f"Outgoing message: {outgoing_message.type}")
                if not self.ws_client:
                    self._log_invalid_config_once(
                        "Cannot send websocket message: hosted Innate agent is not configured "
                        "(missing or placeholder INNATE_SERVICE_KEY)."
                    )
                elif self.ws_client.loop and self.ws_client.loop.is_running():
                    try:
                        asyncio.run_coroutine_threadsafe(self.ws_client.send(outgoing_message), self.ws_client.loop)
                    except RuntimeError as e:
                        self.get_logger().error(f"Error sending message: {e}")
                else:
                    self.get_logger().warn("Cannot send message: WebSocket or event loop not available")
            else:
                self.get_logger().error(f"Unknown message type: {message_type}")
        except Exception as e:
            self.get_logger().error(f"Error processing outgoing message: {e}")

    def run_ws_loop(self, ws_client):
        loop = asyncio.new_event_loop()
        ws_client.loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(ws_client.connect())
        loop.close()


def main(args=None):
    rclpy.init(args=args)
    # Sleep briefly so the WSBridge can send the ready-for-connection message.
    time.sleep(1)
    node = WSClientNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("KeyboardInterrupt, shutting down WSClientNode.")
    finally:
        node.exit_event.set()
        node.destroy_node()
        # Guard against double-shutdown (SIGINT teardown may already have shut the
        # context down; a second shutdown() raises RCLError -> exit 1, masking crashes).
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()

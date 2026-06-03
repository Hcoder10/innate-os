"""WebSocket orchestration, decoupled from any single node.

Owns the connection lifecycle around the :class:`~brain_client.comms.ws_transport.WSClient`
transport: config validation, the reconnect thread, status publishing, robot-version
tracking, live backend-config swaps, and routing of outgoing messages (including the
READY_FOR_CONNECTION connect trigger).

It runs in-process inside ``brain_client_node``: outgoing messages are handed in as
objects via :meth:`handle_outgoing`, and incoming messages are passed to the
``on_incoming`` callback (the brain's ``WSBridge.enqueue_incoming``, which hops them
onto the executor thread). So image/vision payloads never cross a ROS topic or get
re-serialized.

The transport runs the socket on its own asyncio thread and calls back via the small
host interface this implements (``get_logger``, ``exit_event``, ``ws_client``,
``set_ws_status``, ``_handle_ws_error``, ``_last_ws_error_*``, ``forward_incoming``).
``forward_incoming`` is therefore invoked on the asyncio thread, so ``on_incoming``
must only enqueue — never run handlers directly.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

from std_msgs.msg import String

from brain_client.comms.messages import InternalMessage, InternalMessageType, MessageIn
from brain_client.comms.ws_config import is_hosted_innate_uri, validate_token_for_uri, validate_ws_uri
from brain_client.comms.ws_transport import WS_THREAD_STOP_JOIN_SECONDS, WSClient


class WebSocketManager:
    def __init__(self, node, *, uri: str, token: str, client_version: str = "", on_incoming=None):
        self._node = node
        self.on_incoming = on_incoming  # callable(raw_json_str); settable after construction
        self.ws_uri = uri
        self.token = token

        self.exit_event = threading.Event()
        self._ws_stop_event = threading.Event()

        self._ws_status_pub = node.create_publisher(String, "/brain/websocket_status", 10)
        self._ws_status = {
            "state": "starting",
            "connected": False,
            "message": "WebSocket client starting.",
            "uri": self.ws_uri,
            "hosted": False,
            "timestamp": time.time(),
        }
        self._ws_status_timer = node.create_timer(2.0, self._publish_ws_status)
        self._last_backend_unavailable_log_key = None
        self._last_backend_unavailable_log_at = 0.0
        self._last_invalid_config_log_at = 0.0
        self._last_ws_error_message = ""
        self._last_ws_error_at = 0.0

        self._robot_version: str | None = (client_version or "").strip() or None
        self._robot_info_sub = node.create_subscription(String, "/robot/info", self._robot_info_callback, 10)
        if self._robot_version:
            node.get_logger().info(f"Robot version: {self._robot_version}")

        self._tts_pub = node.create_publisher(String, "/brain/tts", 10)
        self._backend_config_sub = node.create_subscription(
            String, "/brain/backend_config", self.backend_config_callback, 10
        )

        self.ws_client = None
        self.ws_thread = None
        self._configure_ws_client(log_invalid=True)

    # --- host interface used by WSClient ---
    def get_logger(self):
        return self._node.get_logger()

    def forward_incoming(self, raw: str) -> None:
        if self.on_incoming is not None:
            self.on_incoming(raw)

    # --- outgoing routing ---
    def handle_outgoing(self, message) -> None:
        """Route an outgoing message object (in-process fast path)."""
        if isinstance(message, InternalMessage):
            self.get_logger().info(f"Internal message: {message}")
            if message.type == InternalMessageType.READY_FOR_CONNECTION:
                self._trigger_connect()
        elif isinstance(message, MessageIn):
            self.get_logger().debug(f"Outgoing message: {message.type}")
            self._send(message)
        else:
            self.get_logger().error(f"Unknown outgoing message: {type(message).__name__}")

    def _trigger_connect(self) -> None:
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
                "invalid_config", False, "Missing or placeholder INNATE_SERVICE_KEY for hosted Innate agent."
            )
            return
        self.get_logger().debug("Received ready for connection message.")
        if self.ws_thread and self.ws_thread.is_alive():
            self.get_logger().debug("WebSocket thread is already running.")
        else:
            self._start_ws_thread()

    def _send(self, message_in) -> None:
        if not self.ws_client:
            self._log_invalid_config_once(
                "Cannot send websocket message: hosted Innate agent is not configured "
                "(missing or placeholder INNATE_SERVICE_KEY)."
            )
        elif self.ws_client.loop and self.ws_client.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.ws_client.send(message_in), self.ws_client.loop)
            except RuntimeError as e:
                self.get_logger().error(f"Error sending message: {e}")
        else:
            self.get_logger().warn("Cannot send message: WebSocket or event loop not available")

    # --- config / thread lifecycle ---
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
                "invalid_config", False, "Missing or placeholder INNATE_SERVICE_KEY for hosted Innate agent."
            )
            return
        self.ws_client = WSClient(self.ws_uri, self.token, self, self._ws_stop_event, self._robot_version)
        if publish_configured_status:
            self.set_ws_status("configured", False, "WebSocket configured; waiting for connection request.")

    def _start_ws_thread(self):
        if self.ws_client is None:
            return
        if self.ws_thread is None or not self.ws_thread.is_alive():
            self.ws_thread = threading.Thread(target=self._run_ws_loop, args=(self.ws_client,))
            self.ws_thread.start()

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

    def _run_ws_loop(self, ws_client):
        loop = asyncio.new_event_loop()
        ws_client.loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(ws_client.connect())
        loop.close()

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

    # --- status / errors ---
    def set_ws_status(self, state: str, connected: bool, message: str):
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
        self._last_ws_error_message = f"{error_type}: {message}"
        self._last_ws_error_at = time.time()
        if error_type == "version_mismatch":
            self._speak_error(message)
            self.get_logger().error(f"Version mismatch: {message}")
            self.set_ws_status("backend_error", False, f"Version mismatch: {message}")
        else:
            self.get_logger().error(f"WebSocket error: {error_type} - {message}")
            self.set_ws_status("backend_error", False, f"Backend error ({error_type}): {message}")

    def shutdown(self):
        self.exit_event.set()
        self._stop_ws_thread()

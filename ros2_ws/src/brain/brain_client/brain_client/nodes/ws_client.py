#!/usr/bin/env python3
"""Standalone WebSocket client node (separate-process / topic-bridge mode).

Thin wrapper around :class:`~brain_client.comms.ws_manager.WebSocketManager`:
incoming backend messages are republished on ``ws_messages`` and outgoing messages
are read from ``ws_outgoing``. This is the legacy split-process transport; by
default ``brain_client_node`` now runs the same manager in-process (see its
``inprocess_websocket`` parameter), so this node only needs launching when fusion
is disabled.
"""

from __future__ import annotations

import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from brain_client.comms.ws_manager import WebSocketManager


class WSClientNode(Node):
    def __init__(self):
        super().__init__("ws_client_node")

        self.declare_parameter("websocket_uri", "ws://localhost:8765")
        self.declare_parameter("token", "MY_HARDCODED_TOKEN")
        self.declare_parameter("client_version", "")
        uri = self.get_parameter("websocket_uri").get_parameter_value().string_value
        token = self.get_parameter("token").get_parameter_value().string_value
        client_version = self.get_parameter("client_version").get_parameter_value().string_value

        self.ws_pub = self.create_publisher(String, "ws_messages", 10)
        self.manager = WebSocketManager(
            self,
            uri=uri,
            token=token,
            client_version=client_version,
            on_incoming=lambda raw: self.ws_pub.publish(String(data=raw)),
        )
        self.create_subscription(String, "ws_outgoing", lambda m: self.manager.handle_outgoing_json(m.data), 10)
        self.get_logger().info("WSClientNode initialized.")

    def destroy_node(self):
        self.manager.shutdown()
        return super().destroy_node()


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
        node.destroy_node()
        # Guard against double-shutdown (SIGINT teardown may already have shut the
        # context down; a second shutdown() raises RCLError -> exit 1, masking crashes).
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()

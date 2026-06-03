#!/usr/bin/env python3
import json

from std_msgs.msg import String

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
    """Bridge between the brain and the WebSocket transport.

    Validates incoming backend messages and dispatches them to registered handlers,
    and serializes/forwards outgoing messages. Two modes:

    - **topic mode** (default): a separate ``ws_client`` process publishes incoming
      JSON on ``ws_messages`` and reads outgoing JSON from ``ws_outgoing``.
    - **in-process mode** (``transport`` given): the bridge drives a
      :class:`~brain_client.comms.ws_manager.WebSocketManager` directly — outgoing
      message *objects* are handed straight to it (no topic, no re-serialization),
      and the manager calls :meth:`dispatch` for incoming messages. Wire it up with
      ``transport.on_incoming = bridge.dispatch`` after construction.
    """

    def __init__(
        self,
        node,
        incoming_topic: str = "ws_messages",
        outgoing_topic: str = "ws_outgoing",
        transport=None,
    ):
        self.node = node
        self.handlers = {}  # Map from MessageOutType to handler callback
        self._transport = transport

        if transport is None:
            self.incoming_sub = self.node.create_subscription(String, incoming_topic, self._ws_callback, 10)
            self.outgoing_pub = self.node.create_publisher(String, outgoing_topic, 10)
            self.node.get_logger().info(
                f"WSBridge subscribed to '{incoming_topic}' and publishing outgoing on '{outgoing_topic}'."
            )
        else:
            self.incoming_sub = None
            self.outgoing_pub = None
            self.node.get_logger().info("WSBridge using in-process WebSocket transport (no topic hop).")

    def register_handler(self, msg_type: MessageOutType, handler):
        """
        Register a (synchronous) handler for a given message out type.
        """
        self.handlers[msg_type] = handler

    def _ws_callback(self, msg: String):
        """Topic-mode subscription callback; delegates to :meth:`dispatch`."""
        self.dispatch(msg.data)

    def dispatch(self, raw: str):
        """Parse, validate, and dispatch one incoming JSON message string.

        Used as the topic callback in topic mode and as the manager's
        ``on_incoming`` callback in in-process mode.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self.node.get_logger().error(f"WSBridge: Failed to parse JSON: {e}")
            return

        # Fast path: check if we have a handler before full validation
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

        # Dispatch to the registered handler for this message type.
        handler = self.handlers.get(ws_msg.type)
        if handler:
            try:
                handler(ws_msg)
            except Exception as e:
                self.node.get_logger().error(f"WSBridge: Handler for {ws_msg.type} raised error: {e}")
        else:
            self.node.get_logger().warn(f"WSBridge: No handler registered for message type: {ws_msg.type}")

    def send_message(self, message):
        """Forward an outgoing message (a MessageIn / InternalMessage instance).

        In-process mode hands the object straight to the transport (one
        serialization, no topic). Topic mode JSON-dumps it onto ``ws_outgoing``.
        """
        if self._transport is not None:
            self._transport.handle_outgoing(message)
            return
        try:
            json_str = message.model_dump_json()
        except Exception as e:
            self.node.get_logger().error(f"WSBridge: Failed to dump message to JSON: {e}")
            return
        self.outgoing_pub.publish(String(data=json_str))

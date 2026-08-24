#!/usr/bin/env python3
"""Say one thing to the brain and print what it actually says back.

The smallest end-to-end test of the real stack: ROS -> brain_client -> Gemini
-> ROS. One model call, so it is also the cheapest way to find out whether the
credential, the transport and the agent loop work together before committing
any budget to a sweep.

TWO GOTCHAS, both of which produced a misleading result first time:

  * /brain/chat_out is LATCHED. A fresh subscriber is handed the last system
    notice ("Brain recovered") the instant it connects, so returning on the
    first message at all reports that instead of the model's answer. This
    waits for a message whose sender is not "system".
  * The brain boots INACTIVE and silently drops chat_in
    ("[BrainClient] Brain is not active. Skipping chat_in message.").
    Call /brain/set_brain_active first.

Run inside the container with the Zenoh RMW set, or it hears nothing:

  docker exec -e RMW_IMPLEMENTATION=rmw_zenoh_cpp innate-dev bash -lc \\
    'source /opt/ros/humble/setup.bash && \\
     source /root/innate-os/ros2_ws/install/setup.bash && \\
     python3 /root/innate-os/workspace/brain_ping.py "hello"'
"""

import json
import sys
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

TIMEOUT_S = 150.0


def is_answer(raw: str) -> bool:
    """A reply from the agent rather than a system notice."""
    try:
        return json.loads(raw).get("sender") not in ("system", None)
    except Exception:  # noqa: BLE001 -- non-JSON is content, not a notice
        return True


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "Hello. In one short sentence, what are you?"

    rclpy.init()
    node = Node("brain_ping")
    seen: list[str] = []
    node.create_subscription(String, "/brain/chat_out", lambda m: seen.append(m.data), 10)
    pub = node.create_publisher(String, "/brain/chat_in", 10)

    deadline = time.time() + 5
    while time.time() < deadline and pub.get_subscription_count() == 0:
        rclpy.spin_once(node, timeout_sec=0.1)
    print(f"brain subscribers on /brain/chat_in: {pub.get_subscription_count()}")

    # Drain whatever was latched before the question is asked, so anything
    # after this point is a response to it.
    drain = time.time() + 2
    while time.time() < drain:
        rclpy.spin_once(node, timeout_sec=0.1)
    seen.clear()

    msg = String()
    msg.data = json.dumps({"text": text})  # a bare string is ignored
    pub.publish(msg)
    print(f"sent: {text!r}\nwaiting up to {TIMEOUT_S:.0f}s...")

    start = time.time()
    while time.time() - start < TIMEOUT_S:
        rclpy.spin_once(node, timeout_sec=0.2)
        if any(is_answer(r) for r in seen):
            break

    elapsed = time.time() - start
    answers = [r for r in seen if is_answer(r)]
    if answers:
        print(f"\nAGENT REPLY after {elapsed:.1f}s:")
        for r in answers:
            print("  " + r[:800])
    else:
        print(f"\nno agent reply in {elapsed:.0f}s; {len(seen)} system message(s):")
        for r in seen[-3:]:
            print("  " + r[:300])
    rclpy.shutdown()
    return 0 if answers else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Arm SDK server — the backend behind the webapp's /armsdk page.

Drives the brain_client Manipulation SDK against the live arm and exposes it
as a tiny localhost JSON API. Launched with the stack (brain_client.launch.py),
so the page is always available; the webapp front door proxies /armsdk/api/*
here, which keeps the raw arm-control port on-box and same-origin.

The Manipulation feeds cost ~600 msg/s of callback dispatch while live, so
they only run while the page does: constructed lazy, started on the first
request, and parked again after IDLE_PARK_S without one (the page polls every
250 ms, so an open tab keeps them warm).

Not skill code — plain time.sleep and blocking calls are fine here.
"""

import json
import math
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rclpy

from brain_client.robot.exceptions import ArmFailed, ArmUnhealthy
from brain_client.robot.manipulation import Manipulation

PORT = 8090
IDLE_PARK_S = 60.0  # park the state feeds this long after the last request

rclpy.init(args=sys.argv)  # honor the launch file's --ros-args
node = rclpy.create_node("arm_sdk_server")
manip = Manipulation(node, node.get_logger(), lazy=True)
manip.safety.max_ee_speed = 0.20  # m/s — stretches cartesian move durations

# One motion at a time; concurrent clicks get a 409 instead of queueing up.
# joints_stream takes it too (briefly): during a blocking move the stream gets
# a 409 and drops the step, so sliders can't fight a discrete motion.
motion_lock = threading.Lock()

# Written on every request, read by the parker thread. Plain float — a torn
# read is impossible under the GIL and a stale one just delays the park a tick.
last_request = time.monotonic()


def touch():
    """Mark activity and make sure the state feeds are live (idempotent)."""
    global last_request
    last_request = time.monotonic()
    if manip._executor is None:  # log the resume, not every poll
        node.get_logger().info("request while parked — starting arm-state feeds")
    manip.start()


def parker():
    """Park the Manipulation feeds once the page has gone quiet."""
    while rclpy.ok():
        time.sleep(5.0)
        if manip._executor is None:  # already parked
            continue
        if time.monotonic() - last_request < IDLE_PARK_S:
            continue
        # Skip while a motion holds the lock — stop() would clear its state
        # out from under it. The lock also serializes us against new commands.
        if motion_lock.acquire(blocking=False):
            try:
                if time.monotonic() - last_request >= IDLE_PARK_S:
                    node.get_logger().info("page idle — parking arm-state feeds")
                    manip.stop()
            finally:
                motion_lock.release()


def arm_to_dict(arm):
    r, p, y = arm.rpy
    return {
        "x": arm.x,
        "y": arm.y,
        "z": arm.z,
        "roll": r,
        "pitch": p,
        "yaw": y,
        "gripper": arm.gripper,
    }


def state():
    msg = manip.last_fk_pose
    pose = arm_to_dict(manip._arm_from_fk(msg)) if msg is not None else None
    js = manip._arm_state
    joints = list(js.position)[:6] if js is not None else None
    return {
        "pose": pose,
        "joints": joints,
        "torque": manip.torque_enabled,
        "moving": manip.moving,
        "grip_target": manip._grip_target,
        "max_ee_speed": manip.safety.max_ee_speed,
    }


def tolerances(body):
    """Verified moves FK-check and auto-recover (servo reboot) on a miss;
    default off for a jog console — the UI shows the settled error instead."""
    if body.get("verify"):
        return {}  # move_to defaults: tolerance_xy=0.05, tolerance_z=0.10
    return {"tolerance_xy": None, "tolerance_z": None}


def do_command(cmd, body):
    if cmd == "joints_stream":
        # Live sliders stream through the SDK's stream_joints() — the teleop
        # pass-through with a velocity-clamped slew, smooth unlike goto calls.
        manip.stream_joints(body["joints"])
        return {}
    # Discrete motions supersede an active stream inside the SDK (_goto
    # calls stream_stop), so no extra guard is needed here.
    if cmd == "move_to":
        x, y, z = float(body["x"]), float(body["y"]), float(body["z"])
        settled = manip.move_to(
            x,
            y,
            z,
            roll=float(body.get("roll", 0.0)),
            pitch=float(body.get("pitch", 0.0)),
            yaw=float(body.get("yaw", 0.0)),
            duration=float(body.get("duration", 1.5)),
            **tolerances(body),
        )
        return {
            "settled": arm_to_dict(settled),
            "target": {"x": x, "y": y, "z": z},
            "err_xy": math.hypot(settled.x - x, settled.y - y),
            "err_z": abs(settled.z - z),
        }

    if cmd == "move_by":
        target = None
        msg = manip.last_fk_pose
        if msg is not None:
            cur = manip._arm_from_fk(msg)
            target = (
                cur.x + float(body.get("dx", 0.0)),
                cur.y + float(body.get("dy", 0.0)),
                cur.z + float(body.get("dz", 0.0)),
            )
        settled = manip.move_by(
            dx=float(body.get("dx", 0.0)),
            dy=float(body.get("dy", 0.0)),
            dz=float(body.get("dz", 0.0)),
            droll=float(body.get("droll", 0.0)),
            dpitch=float(body.get("dpitch", 0.0)),
            dyaw=float(body.get("dyaw", 0.0)),
            duration=float(body.get("duration", 0.8)),
            **tolerances(body),
        )
        out = {"settled": arm_to_dict(settled)}
        if target is not None:
            out["target"] = {"x": target[0], "y": target[1], "z": target[2]}
            out["err_xy"] = math.hypot(settled.x - target[0], settled.y - target[1])
            out["err_z"] = abs(settled.z - target[2])
        return out

    if cmd == "gripper_open":
        manip.gripper_open(float(body.get("percent", 100.0)))
        return {}
    if cmd == "gripper_close":
        manip.gripper_close(float(body.get("strength", 0.0)))
        return {}
    if cmd == "rest":
        manip.rest()
        return {}
    if cmd == "zero":
        manip.move_joints(manip.ZERO, duration=3.0)
        return {}
    if cmd == "torque_on":
        return {"ok": manip.torque_on()}
    if cmd == "torque_off":
        return {"ok": manip.torque_off()}
    if cmd == "recover":
        manip.recover()
        return {}
    if cmd == "speed":
        v = body.get("max_ee_speed")
        manip.safety.max_ee_speed = None if v in (None, "", 0) else float(v)
        return {"max_ee_speed": manip.safety.max_ee_speed}

    raise ArmFailed(f"unknown command {cmd!r}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/state":
            touch()
            self._json(200, state())
        else:
            self._json(404, {"error": "not found — the UI is the webapp's /armsdk page"})

    def do_POST(self):
        if not self.path.startswith("/api/cmd/"):
            self._json(404, {"error": "not found"})
            return
        cmd = self.path[len("/api/cmd/") :]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        touch()

        # torque_off is the abort path: it must work WHILE a motion holds the
        # lock (the motion then fails fast on the de-energized arm).
        needs_lock = cmd != "torque_off"
        if needs_lock and not motion_lock.acquire(blocking=False):
            self._json(409, {"error": "arm busy — wait for the current motion"})
            return
        try:
            result = do_command(cmd, body)
            result["state"] = state()
            self._json(200, result)
        except (ArmFailed, ArmUnhealthy) as e:
            self._json(500, {"error": f"{type(e).__name__}: {e}", "state": state()})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            if needs_lock:
                motion_lock.release()


def main():
    # Localhost only: the webapp front door proxies /armsdk/api/* here, so the
    # raw arm-control API is never exposed off-box.
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=parker, daemon=True).start()
    node.get_logger().info(f"Arm SDK server on http://127.0.0.1:{PORT}")
    # rclpy's signal handlers flip rclpy.ok() on the SIGINT/SIGTERM that
    # ros2 launch sends, so this loop is the shutdown latch either way.
    try:
        while rclpy.ok():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        manip.shutdown()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

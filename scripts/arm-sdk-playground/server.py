#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Arm SDK playground backend — the server behind the webapp's /armsdk page.

Runs the actual brain_client Manipulation SDK (imported from the source tree,
shadowing any installed build) against the live arm, and exposes it as a tiny
localhost JSON API. The webapp front door proxies /armsdk/api/* here, so the
page is same-origin and the raw arm-control port never leaves the robot.

Not skill code — plain time.sleep and blocking calls are fine here.
Run with ./run.sh, then open the webapp's Arm SDK page.
"""

import json
import math
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The point of the playground is to exercise the SDK as it exists in this
# checkout, not whatever build is installed — put the source tree first.
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "brain" / "brain_client"))

import rclpy  # noqa: E402

from brain_client.robot.manipulation import ArmFailed, ArmUnhealthy, Manipulation  # noqa: E402

PORT = 8090

rclpy.init()
node = rclpy.create_node("arm_sdk_playground")
manip = Manipulation(node, node.get_logger())
manip.safety.max_ee_speed = 0.20  # m/s — stretches cartesian move durations

# One motion at a time; concurrent clicks get a 409 instead of queueing up.
# joints_stream takes it too (briefly): during a blocking move the stream gets
# a 409 and drops the step, so sliders can't fight a discrete motion.
motion_lock = threading.Lock()


def arm_to_dict(arm):
    r, p, y = arm.rpy
    return {
        "x": arm.x, "y": arm.y, "z": arm.z,
        "roll": r, "pitch": p, "yaw": y,
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
            x, y, z,
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
            target = (cur.x + float(body.get("dx", 0.0)),
                      cur.y + float(body.get("dy", 0.0)),
                      cur.z + float(body.get("dz", 0.0)))
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
            self._json(200, state())
        else:
            self._json(404, {"error": "not found — the UI is the webapp's /armsdk page"})

    def do_POST(self):
        if not self.path.startswith("/api/cmd/"):
            self._json(404, {"error": "not found"})
            return
        cmd = self.path[len("/api/cmd/"):]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")

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
    print(f"Arm SDK playground on http://127.0.0.1:{PORT}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        manip.shutdown()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

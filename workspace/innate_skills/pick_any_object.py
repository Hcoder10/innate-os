#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Pick any object from the floor, given a text prompt.

The key idea is to localize metrically (pixel -> floor plane -> base_link x,y)
instead of pixel-servoing forever. Once you have a metric fix you can just
drive there and grasp. No depth camera, no hand-eye calibration — just URDF
geometry and a pinhole model.

Validated live on mars-the-44th (2026-07-10): found a sock, grasped it,
proved it kinetically.

Pipeline:

  1. FIND      tilt the head down, ask Gemini for a pixel
  2. LOCALIZE  back-project that pixel onto z=0 (the floor)
  3. POSITION  track the detection point with optical flow and servo the base
               until it sits in a "pick box" on the image (~20 Hz, no Gemini
               in the loop). Re-detect only to confirm / recover from drift.
  4. GRASP     IK hover above the estimate, wrist-align (below), descend,
               close, twist, lift
     WRIST ALIGN go to the operator's posed search position (up high, elbow
               ~90°, wrist camera on the object), get a tight box from
               Gemini, learn a color model of the object vs the floor, then
               segment every wrist frame (~5 Hz) and CamShift-follow the
               blob while stepping the arm down with IK — correcting x/y to
               keep it in the wrist box, descending only while it's inside
               — until wrist_stop_z, where the blind ladder finishes the
               approach and the gripper closes.
  5. VERIFY    back up 15 cm, check the floor is empty (held objects travel
               with the robot). Also check the gripper joint isn't open.

That's it. Everything else is plumbing and knobs.
"""

import base64
import json
import math
import re
import time

import cv2
import numpy as np
from innate_proxy import ProxyClient
from std_msgs.msg import String

from brain_client.skills.types import (
    Interface,
    InterfaceType,
    RobotState,
    RobotStateType,
    Skill,
    SkillResult,
)
from innate.skills import SkillFailed, arm_rest_position
from workspace.skill_lib import arm as armlib
from workspace.skill_lib.geometry import IMG_H, IMG_W, floor_to_pixel, pixel_to_floor

# Vision model, reached through the Innate proxy's OpenAI-compatible endpoint
# (service "gemini", /v1/chat/completions). The proxy holds the upstream key;
# the robot authenticates with its service key. NOTE: the service key must be
# granted access to the "gemini" service or the proxy returns 403.
GEMINI_SERVICE = "gemini"
GEMINI_ENDPOINT = "/v1/chat/completions"
GEMINI_MODEL = "gemini-3.5-flash"

# hardware-tuned constants (not exposed to the live panel)
GRIPPER_EMPTY_J6 = -0.085  # joint6 when fingers are open / empty
VERIFY_BACKUP_M = 0.15     # how far to reverse before asking "is it gone?"

# Carry pose after a successful pick — joints 1-5 posed by the operator. j6 is
# NOT taken from here (the posed value was near-open); the gripper stays firmly
# CLOSED on the sock — see _rest_arm.
CARRY_ARM = [0.0537, -0.5031, 0.4157, 0.9434, -0.0077]

# -----------------------------------------------------------------------------
# live-tunable knobs. Defaults are the hardware-tuned values. The webapp
# pick-tuning panel publishes JSON overrides on TUNING_TOPIC; we apply them
# at the next read, mid-run. Keep keys in sync with pickTunePanel.js.
# -----------------------------------------------------------------------------
TUNABLE = {
    # FIND / LOCALIZE
    "tilt_deg": -20.0,          # head pitch while working (negative = down)
    "settle_s": 1.2,            # wait after stopping before grabbing a frame
    # POSITION — the "pick box" on the image
    "sweet_x": 0.30,            # want the object this far ahead in base_link
    "box_y": 0.0,               # lateral offset of the box centre (drag in UI)
    "box_half_px": 40.0,        # half-width of the goal square, image pixels
    "accept_frac": 0.5,         # stop only when the point is within this
                                #   fraction of box_half_px of the centre — a
                                #   concentric inner box. Accepting anywhere in
                                #   the full box let the arm grasp at an edge,
                                #   short of the object; center it instead.
    "box_steps": 6.0,           # max follow / re-detect attempts
    "bearing_go_deg": 4.0,      # stepwise fallback: fix heading above this
    "follow_gain_ang": 0.3,     # px-servo: rad/s per 100 px of horizontal error
    "follow_gain_lin": 0.06,    # px-servo: m/s per 100 px of vertical error
    # closed-loop base (odometry)
    "rot_tol_deg": 2.5,
    "rot_kp": 1.2,
    "rot_wz_max": 0.5,
    "rot_wz_min": 0.15,         # below this the motors just don't move
    "drive_tol_m": 0.015,
    "drive_kp": 0.3,
    "drive_v_max": 0.10,
    "drive_v_min": 0.04,
    # WRIST ALIGN — visual-servo descent over the wrist camera, starting from
    # the operator's posed search position (WRIST_SEARCH_ARM). Gemini seeds
    # the object pixel, optical flow tracks it on wrist frames while the arm
    # steps down; x/y corrected each cycle, z drops only while inside the box.
    "wrist_steps": 2.0,         # Gemini looks allowed (seed + re-seeds after
                                #   a tracking loss); 0 disables the stage
    "wrist_stop_z": 0.04,       # hand off to the blind descend ladder here
    "wrist_z_step": 0.01,       # descend per verified step, meters
    "wrist_move_s": 0.5,        # per-cycle arm move duration
    "wrist_pitch": 0.82,        # ee pitch during the servo — the operator's
                                #   posed search pose (WRIST_SEARCH_ARM) has
                                #   the wrist camera looking down at exactly
                                #   this pitch. The ladder rotates to
                                #   arm_pitch for the grasp itself; any offset
                                #   that rotation causes is absorbed by where
                                #   the wrist box sits on the image.
    "wrist_box_u": 320.0,       # goal pixel on the wrist image (drag in UI)
    "wrist_box_v": 240.0,
    "wrist_half_px": 60.0,      # accept half-width, pixels
    "wrist_kx": -0.04,          # m forward per 100 px of vertical (v) error
    "wrist_ky": -0.04,          # m left per 100 px of horizontal (u) error.
                                #   Both SIGNED: the wrist cam is mirrored and
                                #   the mount is untested — flip one live if a
                                #   nudge diverges instead of centering. Gains
                                #   are "at z=0.15" and auto-scale with height.
    "wrist_step_max": 0.04,     # single-nudge clamp, meters
    "wrist_settle_s": 0.8,      # settle before a Gemini seed frame
    # GRASP
    "grasp_x_off": 0.03,        # fingertips land ~3 cm forward of ee_link
    "hover_z": 0.15,
    "hover_s": 2.0,
    "descend_z1": 0.10,         # descent ladder, top -> bottom
    "descend_z2": 0.06,
    "descend_z3": 0.03,
    "floor_z": 0.01,            # deep second attempt goes all the way to 0
    "descend_s": 1.2,
    "descend_abort_z": 0.12,    # if EE is still above this, arm is limp — abort
    "arm_pitch": 1.30,
    "close_strength": 0.60,     # firm grip held through the lift. NOT higher:
                                #   0.85 squeezes j6 at max current against finger/
                                #   object contact and OVERCURRENT-TRIPS the gripper
                                #   servo, which then won't open on the next grasp.
    "close_s": 1.5,
    "close_settle_s": 0.8,
    "twist_rad": 0.6,           # wrist roll while closed winds fabric on
    "lift_rad": 0.6,            # shoulder-up joint delta
}

DEBUG_TOPIC = "/pick_any_object/debug"
TUNING_TOPIC = "/pick_any_object/tuning"

FOLLOW_TIMEOUT_S = 20.0
WRIST_ALIGN_TIMEOUT_S = 60.0
WRIST_MAX_JUMP_PX = 80.0
WRIST_SEG_MIN_SCORE = 25.0
WRIST_CAM_ABOVE_EE = 0.07
WRIST_SEARCH_ARM = [0.1473, -0.0706, -0.4449, 1.3376, -0.0491]
# Lucas-Kanade defaults — a small patch beats a single point
_LK_PARAMS = dict(
    winSize=(21, 21), maxLevel=3,
    criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
)


def _r(v, nd=4):
    """round that passes None through (absent sensor readings)."""
    return round(v, nd) if isinstance(v, (int, float)) else None


# -----------------------------------------------------------------------------
# the skill
# -----------------------------------------------------------------------------
class PickAnyObject(Skill):
    """Pick up any object on the floor, specified by a natural-language prompt."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    mobility = Interface(InterfaceType.MOBILITY)
    head = Interface(InterfaceType.HEAD)
    main_image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)
    wrist_image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)
    joint_states = RobotState(RobotStateType.LAST_JOINT_STATES)
    odom = RobotState(RobotStateType.LAST_ODOM)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        # Vision runs through the Innate proxy (upstream key managed there);
        # None when the robot has no proxy credentials configured.
        self._proxy = ProxyClient()
        if not self._proxy.is_available():
            self._proxy = None
        self._p = dict(TUNABLE)  # live copy; panel mutates this
        self._dbg_pub = None
        self._tuning_sub = None

    @property
    def name(self):
        return "pick_any_object"

    def guidelines(self):
        # called on every skill-server load/reload — set up topics early so the
        # webapp panel can subscribe before the first execute()
        self._ensure_debug_io()
        return (
            "Pick up an object lying on the floor, described in natural language "
            "(e.g. prompt='the white sock', 'a red cup'). The robot localizes the "
            "object metrically with the head camera, drives above it, grasps, and "
            "verifies the grasp by backing up and checking the floor. The arm is "
            "returned to rest either way."
        )

    # -------------------------------------------------------------------------
    # debug telemetry + live tuning (webapp pick-tuning panel)
    # -------------------------------------------------------------------------
    def _ensure_debug_io(self):
        # node isn't available in __init__; create pubs/subs once we have it
        if self._dbg_pub is not None or self.node is None:
            return
        self._dbg_pub = self.node.create_publisher(String, DEBUG_TOPIC, 10)
        self._tuning_sub = self.node.create_subscription(
            String, TUNING_TOPIC, self._on_tuning, 10
        )

    def _on_tuning(self, msg):
        try:
            incoming = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        if not isinstance(incoming, dict):
            return
        # only known float knobs; ignore everything else
        applied = {
            k: float(v)
            for k, v in incoming.items()
            if k in self._p and isinstance(v, (int, float))
        }
        if applied:
            self._p.update(applied)
            self.logger.info(f"[PickAnyObject] tuning applied: {applied}")
        # ack with the full live set so the panel can confirm sync
        self._dbg("params", params=dict(self._p))

    def _dbg(self, ev, **fields):
        if self._dbg_pub is None:
            return
        fields.update(t=round(time.time(), 3), ev=ev)
        self._dbg_pub.publish(String(data=json.dumps(fields)))

    # -------------------------------------------------------------------------
    # vision: Gemini finds a pixel, geometry turns it into a floor point
    # -------------------------------------------------------------------------
    def _gemini_call(self, image_b64, question, retries=3):
        """Ask the vision model about an image. Routes through the Innate
        proxy's OpenAI-compatible chat endpoint (image as a data URI); returns
        the reply text, or None after `retries` failures."""
        proxy = self._proxy
        if proxy is None:
            return None
        body = {
            "model": GEMINI_MODEL,
            "temperature": 0.0,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }],
        }
        for attempt in range(retries):
            try:
                with proxy.request_stream(
                    GEMINI_SERVICE, GEMINI_ENDPOINT, method="POST", json=body,
                ) as resp:
                    resp.raise_for_status()
                    data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"] or ""
            except Exception as e:  # noqa: BLE001
                self.logger.warning(
                    f"[PickAnyObject] vision call failed (try {attempt + 1}/{retries}): {e}"
                )
                if attempt < retries - 1:
                    time.sleep(2.0 * (attempt + 1))
        return None

    def _detect_px(self, prompt):
        """One fresh head frame -> best grasp pixel for `prompt`, or None."""
        self._stop_base()
        time.sleep(self._p["settle_s"])  # blur kills detection
        img = self.main_image
        if not img:
            return None
        # ask for the thickest / bunched-up part — that's where two fingers
        # actually get purchase. Flat middle of a sock is a bad pinch.
        text = self._gemini_call(
            img,
            f"Find '{prompt}' lying on the floor in this image. Match precisely — "
            "not paper/packaging when asked for clothing, and NOT anything held "
            "by the robot arm. Return ONLY a JSON list of matches, each "
            '{"box_2d":[ymin,xmin,ymax,xmax], "grasp_point":[y,x]} normalized '
            "0-1000, best first. grasp_point is the THICKEST / most bunched-up / "
            "highest part of the object — the best spot for a two-finger gripper "
            "to pinch, NOT the flat middle. Empty list if not present.",
        )
        return self._parse_det_px(text)

    @staticmethod
    def _parse_dets(text):
        """Gemini detection reply -> list of detection dicts (may be empty)."""
        if not text:
            return []
        text = re.sub(r"```(?:json)?", "", text)
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        try:
            dets = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        return [d for d in dets if isinstance(d, dict)]

    @classmethod
    def _parse_det_px(cls, text):
        """Gemini detection reply -> best (u, v) pixel, or None."""
        for det in cls._parse_dets(text):
            gp = det.get("grasp_point")
            if gp and len(gp) >= 2:
                # Gemini coords are [y,x] in 0..1000 — flip to (u,v) pixels
                return (float(gp[1]) / 1000.0 * IMG_W, float(gp[0]) / 1000.0 * IMG_H)
            b = det.get("box_2d")
            if b and len(b) >= 4:
                y0, x0, y1, x1 = [float(v) for v in b[:4]]
                y0, y1 = sorted((y0, y1))
                x0, x1 = sorted((x0, x1))
                return ((x0 + x1) / 2 / 1000.0 * IMG_W, (y0 + y1) / 2 / 1000.0 * IMG_H)
        return None

    @classmethod
    def _parse_det_box(cls, text):
        """Gemini detection reply -> best pixel box (x, y, w, h), or None."""
        for det in cls._parse_dets(text):
            b = det.get("box_2d")
            if b and len(b) >= 4:
                y0, x0, y1, x1 = [float(v) for v in b[:4]]
                y0, y1 = sorted((y0, y1))
                x0, x1 = sorted((x0, x1))
                x = int(x0 / 1000.0 * IMG_W)
                y = int(y0 / 1000.0 * IMG_H)
                w = int((x1 - x0) / 1000.0 * IMG_W)
                h = int((y1 - y0) / 1000.0 * IMG_H)
                if w >= 8 and h >= 8:  # a sliver can't seed a color model
                    return (x, y, w, h)
        return None

    def _localize(self, prompt):
        """(x, y) of the object in base_link, or None."""
        return self._localize_px(prompt)[0]

    def _localize_px(self, prompt):
        """Detect + back-project. Returns ((x,y)|None, pixel|None)."""
        px = self._detect_px(prompt)
        if px is None:
            self._dbg("localize", px=None, xy=None)
            return None, None
        xy = pixel_to_floor(px[0], px[1], self._p["tilt_deg"])
        if xy:
            self.logger.info(
                f"[PickAnyObject] px=({px[0]:.0f},{px[1]:.0f}) -> "
                f"base_link ({xy[0]:.3f},{xy[1]:.3f})"
            )
        self._dbg(
            "localize",
            px=[round(px[0], 1), round(px[1], 1)],
            xy=[round(xy[0], 4), round(xy[1], 4)] if xy else None,
            tilt_deg=self._p["tilt_deg"],
        )
        return xy, px

    # -------------------------------------------------------------------------
    # base control, closed-loop on odometry
    # when odom is missing we fall back to timed open-loop — never a silent no-op
    # -------------------------------------------------------------------------
    def _stop_base(self):
        self.mobility.send_cmd_vel(0.0, 0.0, 0.1)

    def _odom_xyt(self):
        """(x, y, theta). Accepts both the nested dict and the flat dataclass."""
        od = self.odom
        if od is None:
            return None
        try:
            if isinstance(od, dict):
                p = od["pose"]["pose"]["position"]
                return (float(p["x"]), float(p["y"]),
                        math.radians(float(od["theta_degrees"])))
            return (float(od.x), float(od.y), float(od.theta))
        except (KeyError, AttributeError, TypeError):
            return None

    def _rotate_by(self, angle, tol=None, timeout=12.0):
        self._dbg("rotate", angle_deg=round(math.degrees(angle), 1))
        od = self._odom_xyt()
        if od is None:
            # poor man's rotate: open-loop timed yaw
            self.mobility.send_cmd_vel(0.0, math.copysign(0.35, angle),
                                       abs(angle) / 0.35)
            time.sleep(abs(angle) / 0.35 + 0.4)
            return
        target = od[2] + angle
        t0 = time.time()
        err = angle
        while time.time() - t0 < timeout and not self._cancelled:
            od = self._odom_xyt()
            if od is None:
                break
            # wrap to [-pi, pi]
            err = math.atan2(math.sin(target - od[2]), math.cos(target - od[2]))
            if abs(err) < (tol if tol is not None
                           else math.radians(self._p["rot_tol_deg"])):
                break
            wz_max = self._p["rot_wz_max"]
            wz = max(-wz_max, min(wz_max, self._p["rot_kp"] * err))
            if abs(wz) < self._p["rot_wz_min"]:
                wz = math.copysign(self._p["rot_wz_min"], wz)
            self.mobility.send_cmd_vel(0.0, wz, 0.15)
            time.sleep(0.08)
        self._stop_base()
        self._dbg("rotate_done", err_deg=round(math.degrees(err), 2),
                  s=round(time.time() - t0, 2))

    def _drive(self, dist, tol=None, timeout=15.0):
        tol = tol if tol is not None else self._p["drive_tol_m"]
        if abs(dist) < tol:
            return
        self._dbg("drive", dist_m=round(dist, 3))
        od = self._odom_xyt()
        if od is None:
            self.logger.warning("[PickAnyObject] no odom — open-loop drive")
            self.mobility.send_cmd_vel(math.copysign(0.08, dist), 0.0,
                                       abs(dist) / 0.08)
            time.sleep(abs(dist) / 0.08 + 0.4)
            return
        x0, y0 = od[0], od[1]
        t0 = time.time()
        err = abs(dist)
        while time.time() - t0 < timeout and not self._cancelled:
            od = self._odom_xyt()
            if od is None:
                break
            gone = math.hypot(od[0] - x0, od[1] - y0)
            err = abs(dist) - gone
            if err < tol:
                break
            v = math.copysign(
                max(self._p["drive_v_min"],
                    min(self._p["drive_v_max"], self._p["drive_kp"] * err)),
                dist,
            )
            self.mobility.send_cmd_vel(v, 0.0, 0.15)
            time.sleep(0.08)
        self._stop_base()
        self._dbg("drive_done", err_m=round(err, 4), s=round(time.time() - t0, 2))

    # -------------------------------------------------------------------------
    # arm. Health-checked moves, recovery, and reach clamps live in
    # workspace/skill_lib/arm.py (armlib) — thin state accessors stay here.
    # -------------------------------------------------------------------------
    def _ee_xyz(self):
        return armlib.ee_xyz(self.manipulation)

    def _gripper_j6(self):
        return armlib.gripper_j6(self.joint_states)

    def _rest_arm(self, keep_grip):
        """Fold to home. If carrying: go to the operator's CARRY_ARM pose with
        the gripper held firmly CLOSED (j6 = -close_strength). Command the full
        close, not the achieved stalled-on-object value, so the squeeze holds
        while carrying — otherwise the grip relaxes and the sock slips."""
        if keep_grip:
            target = CARRY_ARM + [-self._p["close_strength"]]
        else:
            target = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        for _ in range(2):
            self.manipulation.move_to_joint_positions(
                joint_positions=target, duration=3, blocking=True,
            )
            time.sleep(0.3)

    # -------------------------------------------------------------------------
    # stages
    # -------------------------------------------------------------------------
    def _search(self, prompt):
        """Look straight, then right 30°, then left 60°. First hit wins."""
        # +yaw = left (CCW), -yaw = right (CW)
        for i, turn in enumerate((0.0, -math.radians(30), math.radians(60))):
            if self._cancelled:
                return None
            if turn:
                if i == 1:
                    self.say("Scanning around for it.")
                self._rotate_by(turn)
            xy = self._localize(prompt)
            if xy is not None:
                return xy
        return None

    def _sweet_box(self):
        """Pick-box centre (from sweet_x, box_y), outer half-width in pixels,
        and the inner accept half-width (accept_frac * outer). The base stops
        only once the tracked point sits inside the INNER box — accepting it
        anywhere in the outer box let the arm grasp at an edge, short of the
        object. The webapp draws both squares from the same params."""
        c = floor_to_pixel(self._p["sweet_x"], self._p["box_y"],
                           self._p["tilt_deg"])
        half = self._p["box_half_px"]
        return c, half, half * self._p["accept_frac"]

    # ---- detection-point following ------------------------------------------
    # Gemini is slow (~1s). Optical flow is free. So: detect once, track the
    # pixel at camera rate, and P-servo the base until it's in the pick box.
    # -------------------------------------------------------------------------
    def _gray_frame(self):
        img = self.main_image
        if not img:
            return None
        try:
            arr = np.frombuffer(base64.b64decode(img), np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        except Exception:  # noqa: BLE001 — a bad frame just skips a cycle
            return None

    @staticmethod
    def _grid_pts(u, v, step=12, n=2):
        """(2n+1)^2 feature points around (u,v). A patch tracks; a point doesn't."""
        pts = [[u + dx * step, v + dy * step]
               for dx in range(-n, n + 1) for dy in range(-n, n + 1)]
        return np.array(pts, dtype=np.float32).reshape(-1, 1, 2)

    def _track_point(self, prev_gray, gray, grid):
        """LK one step. Median of survivors = object's new pixel. None if lost."""
        nxt, status, _ = cv2.calcOpticalFlowPyrLK(
            prev_gray, gray, grid, None, **_LK_PARAMS)
        if nxt is None or status is None:
            return None
        good = nxt[status.flatten() == 1].reshape(-1, 2)
        if len(good) < 3:
            return None
        return float(np.median(good[:, 0])), float(np.median(good[:, 1]))

    def _follow_into_box(self, seed_px):
        """High-rate follow loop. No Gemini. Returns one of:
        ('in_box', (u,v)) | ('lost', None) | ('timeout', None) | ('noframe', None)
        """
        prev = self._gray_frame()
        if prev is None:
            return "noframe", None
        u, v = seed_px
        grid = self._grid_pts(u, v)
        in_box, last_dbg = 0, 0.0
        t0 = time.time()
        while time.time() - t0 < FOLLOW_TIMEOUT_S and not self._cancelled:
            gray = self._gray_frame()
            if gray is None:
                time.sleep(0.03)
                continue
            tracked = self._track_point(prev, gray, grid)
            prev = gray
            if tracked is None:
                self._stop_base()
                return "lost", None
            u, v = tracked
            grid = self._grid_pts(u, v)  # recenter the patch
            if not (0 <= u < IMG_W and 0 <= v < IMG_H):
                self._stop_base()
                return "lost", None

            (cu, cv), half, accept = self._sweet_box()  # re-read: box moves live
            eu, ev = u - cu, v - cv
            inside = abs(eu) <= accept and abs(ev) <= accept

            now = time.time()
            if now - last_dbg > 0.1:  # ~10 Hz marker for the webapp
                self._dbg("servo", px=[round(u, 1), round(v, 1)],
                          box=[round(cu, 1), round(cv, 1), half,
                               round(accept, 1)], inside=inside)
                last_dbg = now

            if inside:
                in_box += 1
                self._stop_base()
                if in_box >= 3:  # debounce a few frames
                    return "in_box", (u, v)
                time.sleep(0.03)
                continue
            in_box = 0

            # per-axis pixel servo. object right of box -> turn CW (-wz);
            # object low in the image (too close) -> back up (-vx).
            # gains are "per 100 px" so the numbers stay human-sized.
            # Deadband on the INNER accept box, not the outer one — otherwise the
            # servo would stop driving at the outer edge and never reach accept.
            if abs(eu) <= accept:
                wz = 0.0
            else:
                wz = max(-self._p["rot_wz_max"],
                         min(self._p["rot_wz_max"],
                             -self._p["follow_gain_ang"] / 100.0 * eu))
                if abs(wz) < self._p["rot_wz_min"]:
                    wz = math.copysign(self._p["rot_wz_min"], wz)
            if abs(ev) <= accept:
                vx = 0.0
            else:
                vx = max(-self._p["drive_v_max"],
                         min(self._p["drive_v_max"],
                             -self._p["follow_gain_lin"] / 100.0 * ev))
                if abs(vx) < self._p["drive_v_min"]:
                    vx = math.copysign(self._p["drive_v_min"], vx)
            self.mobility.send_cmd_vel(vx, wz, 0.15)
            time.sleep(0.03)
        self._stop_base()
        return "timeout", None

    def _position_above(self, prompt, xy):
        """Get the object into the pick box via optical-flow following.
        Gemini reseeds after a tracking loss and confirms at the end.
        Falls back to discrete stepwise corrections if there's no camera."""
        if self._gray_frame() is None:
            return self._position_stepwise(prompt, xy)

        # seed from the caller's fix — no extra Gemini call
        seed = floor_to_pixel(xy[0], xy[1], self._p["tilt_deg"])
        for attempt in range(int(self._p["box_steps"])):
            if self._cancelled:
                return None
            if seed is None:
                seed = self._detect_px(prompt)
                if seed is None:
                    self._dbg("position_done", xy=None, reason="lost")
                    return None
            result, _pt = self._follow_into_box(seed)
            if result == "noframe":
                return self._position_stepwise(prompt, xy)
            if result == "lost":
                seed = None  # re-detect and try again
                continue
            # in_box or timeout — confirm with one real detection
            xy2, px2 = self._localize_px(prompt)
            if px2 is None:
                self._dbg("position_done", xy=None, reason="lost")
                return None
            (cu, cv), half, accept = self._sweet_box()
            if (xy2 is not None
                    and abs(px2[0] - cu) <= accept and abs(px2[1] - cv) <= accept):
                self._dbg("position_done",
                          xy=[round(xy2[0], 4), round(xy2[1], 4)], attempts=attempt)
                return xy2
            seed = px2 if xy2 is not None else None
        self._dbg("position_done", xy=None, reason="never inside the box")
        return None

    def _position_stepwise(self, prompt, xy):
        """Odometry-free / no-camera fallback: one discrete correction per look.
        Turn OR drive (never both), re-detect, repeat."""
        target_bearing = math.atan2(self._p["box_y"], self._p["sweet_x"])
        target_range = math.hypot(self._p["sweet_x"], self._p["box_y"])
        px = floor_to_pixel(xy[0], xy[1], self._p["tilt_deg"])
        for step in range(int(self._p["box_steps"])):
            if self._cancelled:
                return None
            if px is None:
                xy, px = self._localize_px(prompt)
                if px is None:
                    self._dbg("position_done", xy=None, reason="lost")
                    return None
            (cu, cv), half, accept = self._sweet_box()
            inside = (xy is not None
                      and abs(px[0] - cu) <= accept and abs(px[1] - cv) <= accept)
            self._dbg("position", step=step,
                      px=[round(px[0], 1), round(px[1], 1)],
                      xy=[round(xy[0], 4), round(xy[1], 4)] if xy else None,
                      box=[round(cu, 1), round(cv, 1), half, round(accept, 1)],
                      inside=inside)
            if inside:
                self._dbg("position_done",
                          xy=[round(xy[0], 4), round(xy[1], 4)], steps=step)
                return xy
            if xy is None:  # pixel above the horizon — bogus, retry
                px = None
                continue
            bearing_err = math.atan2(xy[1], xy[0]) - target_bearing
            if abs(bearing_err) > math.radians(self._p["bearing_go_deg"]):
                self._rotate_by(bearing_err)
            else:
                self._drive(math.hypot(xy[0], xy[1]) - target_range)
            px = None  # we moved; re-detect before the next correction
        self._dbg("position_done", xy=None, reason="never inside the box")
        return None

    # -------------------------------------------------------------------------
    # GRASP — everything after the base is parked. Five moves, top to bottom:
    #
    #   1. search pose  joint move to the operator's posed arm: up high,
    #                   elbow ~90°, wrist camera looking down at the floor
    #   2. seed         ask Gemini for a tight box around the object in the
    #                   wrist image; learn a color model of it vs the floor
    #   3. servo down   segment every frame and CamShift-follow the blob;
    #                   after two verified frames, center it or descend 1 cm
    #   4. final push   below wrist_stop_z the camera is too close to help —
    #                   rotate to the grasp pitch and press to the floor
    #   5. close        squeeze, twist the fabric on, lift
    #
    # Steps 1-3 are skipped when wrist_steps is 0 (classic blind grasp).
    # -------------------------------------------------------------------------
    def _ee_dbg(self):
        ee = self._ee_xyz()
        return [round(v, 4) for v in ee] if ee else None

    def _wrist_seed(self, prompt, z):
        """Gemini, where is the object in the wrist image? Returns
        (center, box) with box = (x, y, w, h) pixels, or (None, None).
        Asks for a TIGHT bounding box — its center is the object's center,
        and the box itself seeds the segmentation color model. Gemini places
        boxes much more reliably than point guesses. Same 640x480 as head."""
        time.sleep(self._p["wrist_settle_s"])  # let the arm stop swaying
        img = self.wrist_image
        text = self._gemini_call(
            img,
            f"Wrist camera on a robot gripper, looking down at the floor. "
            f"Find '{prompt}' on the floor. Ignore the gripper fingers "
            "themselves. Return ONLY a JSON list of matches, each "
            '{"box_2d":[ymin,xmin,ymax,xmax]} normalized 0-1000, best first, '
            "each box TIGHT around its object. Empty list if not visible.",
        ) if img else None
        box = self._parse_det_box(text)
        px = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0) if box else None
        self._dbg("wrist_seed", z=round(z, 3),
                  px=[round(px[0], 1), round(px[1], 1)] if px else None)
        return px, box

    def _next_wrist_hsv(self, last_b64, timeout=1.5):
        """Wait for a NEW wrist frame (the topic is ~5 Hz) and decode it to
        HSV. Returns (hsv|None, b64) — b64 unchanged on timeout."""
        t0 = time.time()
        while time.time() - t0 < timeout and not self._cancelled:
            img = self.wrist_image
            if img and img != last_b64:
                try:
                    arr = np.frombuffer(base64.b64decode(img), np.uint8)
                    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                except Exception:  # noqa: BLE001 — a bad frame just skips
                    bgr = None
                if bgr is not None:
                    return cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV), img
            time.sleep(0.04)
        return None, last_b64

    # Why segmentation instead of point tracking: during the descent the
    # object grows ~2.5x in the image and the fabric deforms — optical-flow
    # patches don't survive that and slide onto the carpet. A color model is
    # scale- and deformation-proof: learn what the object looks like COMPARED
    # to the floor right around it, then find that blob in every frame.
    _SEG_BINS = [16, 8, 8]                    # H, S, V histogram bins
    _SEG_RANGES = [0, 180, 0, 256, 0, 256]

    def _seg_model(self, hsv, box):
        """Likelihood-ratio color model from the seed box: histogram of the
        object over histogram of a surrounding ring of floor. Bins where the
        object beats the floor get high weight, shared colors get ~0 — so
        back-projection lights up the object and stays dark on carpet.
        Returns a uint8 [16,8,8] lookup table, or None if degenerate."""
        x, y, w, h = box
        obj = hsv[y:y + h, x:x + w]
        rx0, ry0 = max(0, x - w // 2), max(0, y - h // 2)
        ring = hsv[ry0:y + h + h // 2, rx0:x + w + w // 2]
        h_obj = cv2.calcHist([obj], [0, 1, 2], None,
                             self._SEG_BINS, self._SEG_RANGES)
        h_ring = cv2.calcHist([ring], [0, 1, 2], None,
                              self._SEG_BINS, self._SEG_RANGES)
        ratio = h_obj / (np.maximum(h_ring - h_obj, 0.0) + 1.0)
        if ratio.max() <= 0:
            return None
        return (255.0 * ratio / ratio.max()).astype(np.uint8)

    def _seg_track(self, hsv, model, window):
        """One segmentation step: back-project the color model (a direct
        numpy bin lookup — cv2.calcBackProject silently returns zeros for
        3-D histograms on this build) and let CamShift follow the blob from
        the previous window, adapting it as the object grows during the
        descent. Returns (center|None, window, score) — None when the
        likelihood collapsed (lost / occluded) or the window degenerated."""
        ih = (hsv[:, :, 0].astype(np.int32) * self._SEG_BINS[0]) // 180
        i_s = (hsv[:, :, 1].astype(np.int32) * self._SEG_BINS[1]) // 256
        iv = (hsv[:, :, 2].astype(np.int32) * self._SEG_BINS[2]) // 256
        bp = model[np.clip(ih, 0, self._SEG_BINS[0] - 1), i_s, iv]
        crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 1)
        _rot, window = cv2.CamShift(bp, window, crit)
        x, y, w, h = window
        if w < 4 or h < 4 or w * h > 0.4 * IMG_W * IMG_H:
            return None, window, 0.0
        score = float(bp[y:y + h, x:x + w].mean())
        if score < WRIST_SEG_MIN_SCORE:
            return None, window, score
        return (x + w / 2.0, y + h / 2.0), window, score

    def _wrist_servo(self, prompt, tx, ty):
        """Servo the arm down over the object, wrist camera as the eye.

        Watch, then act. Watch: segment the object with a color model
        (learned once from the Gemini seed box against the floor around it)
        and follow the blob with CamShift until two consecutive frames
        agree, arm still. Act: one move — center the blob (nudge x/y toward
        the wrist box) if it's off, and only once it has been seen INSIDE
        the box on two consecutive frames descend one wrist_z_step. Center
        first, then step down; repeat until wrist_stop_z. A failed match
        gets two frames of patience (blur, mid-move frames, occlusion); a
        persistent loss spends one Gemini look to re-find the object and
        relearn the model (wrist_steps looks total, seed included).

        Returns (x, y, z) for the final push. If the object was never seen,
        the caller's (tx, ty) come back unchanged so the push falls back on
        the head-camera estimate."""
        p = self._p
        ee = self._ee_xyz()
        z = ee[2] if ee else p["hover_z"]
        looks = int(p["wrist_steps"]) - 1  # Gemini budget after the seed

        px, box = self._wrist_seed(prompt, z)
        if px is None:
            self._dbg("wrist_done", tx=round(tx, 4), ty=round(ty, 4),
                      z=round(z, 3), reason="not seen")
            return tx, ty, z
        x, y = (ee[0], ee[1]) if ee else (tx, ty)  # servo from the real pose

        hsv, raw = self._next_wrist_hsv(None)
        if hsv is None:
            self._dbg("wrist_done", tx=round(tx, 4), ty=round(ty, 4),
                      z=round(z, 3), reason="no wrist frames")
            return tx, ty, z
        model = self._seg_model(hsv, box)  # what the object looks like here
        if model is None:
            self._dbg("wrist_done", tx=round(tx, 4), ty=round(ty, 4),
                      z=round(z, 3), reason="not seen")
            return tx, ty, z
        window = box
        guess = px  # where we expect the blob in the NEXT frame
        deadline = time.time() + WRIST_ALIGN_TIMEOUT_S
        streak = 0    # verified matches since the arm last moved
        centered = 0  # consecutive verified matches INSIDE the box
        misses = 0    # consecutive failed matches (blur, mid-move, occlusion)
        reason = "reached stop z"
        while z > p["wrist_stop_z"] + 1e-6:
            if self._cancelled:
                reason = "cancelled"
                break
            if time.time() > deadline:
                reason = "timeout"
                break
            hsv, raw = self._next_wrist_hsv(raw)
            if hsv is None:
                reason = "no wrist frames"
                break

            pt, window, _score = self._seg_track(hsv, model, window)
            if pt is not None and math.hypot(
                    pt[0] - guess[0], pt[1] - guess[1]) > WRIST_MAX_JUMP_PX:
                pt = None  # a blob elsewhere isn't our object
            if pt is None:
                streak = 0
                centered = 0
                misses += 1
                if misses < 3:
                    window = box  # retry from the last trusted window
                    continue  # transient (blurred / mid-move frame) — wait
                if looks <= 0:  # persistent loss — worth one Gemini look?
                    reason = "lost track"
                    break
                looks -= 1
                pt, box = self._wrist_seed(prompt, z)
                if pt is None:
                    reason = "lost track"
                    break
                hsv, raw = self._next_wrist_hsv(raw)
                if hsv is None:
                    reason = "no wrist frames"
                    break
                model = self._seg_model(hsv, box)  # the view changed: relearn
                if model is None:
                    reason = "lost track"
                    break
                window = box
            misses = 0
            box = window  # last trusted window
            px = pt
            guess = px
            streak += 1

            err_u = px[0] - p["wrist_box_u"]
            err_v = px[1] - p["wrist_box_v"]
            inside = (abs(err_u) <= p["wrist_half_px"]
                      and abs(err_v) <= p["wrist_half_px"])
            centered = centered + 1 if inside else 0
            self._dbg("wrist_servo", px=[round(px[0], 1), round(px[1], 1)],
                      z=round(z, 3), inside=inside,
                      tx=round(x, 4), ty=round(y, 4), ee=self._ee_dbg())
            if streak < 2:
                continue  # watch one more frame before trusting it
            if inside and centered < 2:
                continue  # just entered the box — confirm it stays centered

            if centered >= 2:
                z = max(p["wrist_stop_z"], z - p["wrist_z_step"])
                # descending barely moves the (centered) pixel: guess stays px
            else:
                # px -> meters: gains are tuned at z=0.15 and shrink as the
                # camera nears the floor, so scale by the current height.
                s = (z + WRIST_CAM_ABOVE_EE) / (0.15 + WRIST_CAM_ABOVE_EE)
                cap = p["wrist_step_max"]
                x += max(-cap, min(cap, p["wrist_kx"] / 100.0 * err_v * s))
                y += max(-cap, min(cap, p["wrist_ky"] / 100.0 * err_u * s))
                x, y = armlib.clamp_reach(x, y)
                # feed-forward: this correction should land the object at the
                # box center — tell the tracker to look for it there
                guess = (p["wrist_box_u"], p["wrist_box_v"])
            armlib.move_checked(self.manipulation, x, y, z,
                                pitch=p["wrist_pitch"],
                                duration=p["wrist_move_s"], logger=self.logger)
            streak = 0
            centered = 0  # the view shifted — re-confirm centering

        grab = floor_to_pixel(x + p["grasp_x_off"], y, p["tilt_deg"])
        self._dbg("wrist_done", tx=round(x, 4), ty=round(y, 4), z=round(z, 3),
                  reason=reason,
                  grab_px=[round(grab[0], 1), round(grab[1], 1)]
                  if grab else None)
        return x, y, z

    def _goto_search_pose(self, bearing):
        """Joint move to the operator's posed search arm (WRIST_SEARCH_ARM):
        up high, elbow ~90°, wrist camera on the floor ahead. j1 aims along
        `bearing`, j4 completes the camera pitch (j2+j3+j4 = wrist_pitch),
        the gripper keeps its position. Starting here also pins the IK to
        the elbow-up branch — LMA converges to the solution nearest the
        current joints — so the whole descent keeps this arm shape."""
        a = WRIST_SEARCH_ARM
        j6 = self._gripper_j6()
        pose = [bearing, a[1], a[2],
                self._p["wrist_pitch"] - a[1] - a[2], a[4],
                j6 if j6 is not None else 0.0]
        self.manipulation.move_to_joint_positions(
            joint_positions=pose, duration=self._p["hover_s"], blocking=True)
        time.sleep(0.3)
        self._dbg("hover", ee=self._ee_dbg())

    def _open_gripper_checked(self):
        """Open the claw, verified (trip recovery) — see armlib.open_checked."""
        armlib.open_checked(
            self.manipulation, self._gripper_j6, logger=self.logger,
            on_reboot=lambda j6: self._dbg("gripper_reboot", j6=_r(j6)),
        )

    def _push_to_floor(self, x, y, z_from, deep):
        """The last blind centimeters: rotate to the grasp pitch and descend
        rung by rung to the floor. A rung that fails is floor contact /
        overload — recover and grasp there. Raises if the arm never got low
        enough for a grasp to make sense."""
        p = self._p
        rungs = [p["descend_z1"], p["descend_z2"], p["descend_z3"],
                 0.0 if deep else p["floor_z"]]
        for z in rungs:
            if z >= z_from - 1e-6:  # the servo already descended past this
                continue
            if self._cancelled:
                return
            ok = self.manipulation.move_to_cartesian_pose(
                x=x, y=y, z=z, roll=0.0, pitch=p["arm_pitch"], yaw=0.0,
                duration=p["descend_s"], blocking=True,
            )
            self._dbg("descend", z=round(z, 3), ok=bool(ok), ee=self._ee_dbg())
            if not ok:
                armlib.recover(self.manipulation, self.logger)
                break
        ee = self._ee_xyz()
        if ee is not None and ee[2] > p["descend_abort_z"]:
            self._dbg("grasp_abort", reason="arm would not descend",
                      ee_z=round(ee[2], 4))
            armlib.recover(self.manipulation, self.logger)
            raise armlib.ArmUnhealthy("arm would not descend")

    def _close_twist_lift(self, x, y):
        """Take it: squeeze, wind the fabric onto the fingers, lift. Twist
        and lift happen in JOINT space — an IK lift would recompute joint5
        and unwind the object off the fingers. j6 holds the FULL close
        command throughout: re-commanding the achieved (stalled-on-object)
        position instead relaxes the squeeze mid-lift and drops socks."""
        p = self._p
        j6_open = self._gripper_j6()
        armlib.close(self.manipulation, strength=p["close_strength"],
                     duration=p["close_s"])
        time.sleep(p["close_settle_s"])
        self._dbg("close", j6_open=_r(j6_open), j6=_r(self._gripper_j6()),
                  strength=p["close_strength"])

        grip = -p["close_strength"]
        try:
            j = list(self.joint_states["position"][:6])
            j[4] = max(-1.4, min(1.4, j[4] + p["twist_rad"]))
            j[5] = grip
            self.manipulation.move_to_joint_positions(
                joint_positions=j, duration=1.0, blocking=True)
            time.sleep(0.3)
            self._dbg("twist", j4=round(j[4], 3))
            j = list(self.joint_states["position"][:6])
            j[1] = max(-1.4, j[1] - p["lift_rad"])  # shoulder up = lift
            j[5] = grip
            self.manipulation.move_to_joint_positions(
                joint_positions=j, duration=2.0, blocking=True)
            time.sleep(0.3)
            self._dbg("lift", ee=self._ee_dbg(), j6=_r(self._gripper_j6()))
        except (KeyError, IndexError, TypeError):  # no joint state — IK lift
            armlib.move_checked(self.manipulation, x, y, 0.22,
                                pitch=p["arm_pitch"], duration=2.0, tol=0.10,
                                logger=self.logger)
            self._dbg("lift", ee=self._ee_dbg(), j6=_r(self._gripper_j6()),
                      ik_fallback=True)

    def _grasp_at(self, prompt, xy, deep=False):
        """The grasp, start to finish — see the section comment above. `xy`
        is the object on the floor in base_link. Every number reads self._p
        so the panel can retune a grasp mid-run."""
        p = self._p
        # aim short: fingertips land ~grasp_x_off forward of ee_link
        x, y = armlib.clamp_reach(xy[0] - p["grasp_x_off"], xy[1])

        # telemetry: where the object is vs where the fingers will land (they
        # differ only when the object is outside the reach box — aim clamped)
        grab_px = floor_to_pixel(x + p["grasp_x_off"], y, p["tilt_deg"])
        obj_px = floor_to_pixel(xy[0], xy[1], p["tilt_deg"])
        clamped = (abs((xy[0] - p["grasp_x_off"]) - x) > 1e-4
                   or abs(xy[1] - y) > 1e-4)
        self._dbg("grasp", xy=[round(xy[0], 4), round(xy[1], 4)],
                  tx=round(x, 4), ty=round(y, 4), deep=deep,
                  grab_px=[round(grab_px[0], 1), round(grab_px[1], 1)]
                  if grab_px else None,
                  obj_px=[round(obj_px[0], 1), round(obj_px[1], 1)]
                  if obj_px else None,
                  clamped=clamped)

        self._open_gripper_checked()

        if p["wrist_steps"] >= 1:
            self._goto_search_pose(math.atan2(y, x))
            x, y, z = self._wrist_servo(prompt, x, y)
        else:  # classic blind grasp: IK hover above the estimate
            z = p["hover_z"]
            armlib.move_checked(self.manipulation, x, y, z,
                                pitch=p["arm_pitch"], duration=p["hover_s"],
                                logger=self.logger)
            self._dbg("hover", ee=self._ee_dbg())

        self._push_to_floor(x, y, z, deep)
        if self._cancelled:
            return
        self._close_twist_lift(x, y)

    # -------------------------------------------------------------------------
    # VERIFY — did we actually get it?
    # -------------------------------------------------------------------------
    def _grasp_verified(self, prompt):
        """Back up so a held object travels with us, then ask the head camera
        if the floor is clear. (Wrist cam is mirrored — don't trust it.)"""
        self._drive(-VERIFY_BACKUP_M)
        time.sleep(self._p["settle_s"])
        j6 = self._gripper_j6()
        img = self.main_image
        floor_text = self._gemini_call(
            img,
            f"Robot head camera looking at the floor. Is '{prompt}' lying ON the "
            "floor/carpet anywhere in view? Something hanging from the robot's "
            "gripper does NOT count as on the floor. Answer only YES or NO.",
        ) if img else None
        floor_clear = bool(floor_text) and "NO" in floor_text.upper()
        j6_ok = j6 is not None and j6 > GRIPPER_EMPTY_J6 + 0.02
        held = floor_clear and j6_ok
        self.logger.info(
            f"[PickAnyObject] verify: floor={floor_text!r} j6={j6} "
            f"-> {'HELD' if held else 'NOT HELD'}"
        )
        self._dbg("verify", floor_clear=floor_clear, j6=_r(j6), held=held)
        return held

    # -------------------------------------------------------------------------
    # entry point
    # -------------------------------------------------------------------------
    def execute(self, prompt: str = "the sock", max_base_steps: int = 10):
        """
        Pick up the object described by `prompt` from the floor.

        Args:
            prompt: Natural-language description, e.g. "the white sock".
            max_base_steps: kept for interface compatibility; unused in v3.
        """
        self._cancelled = False
        if self._proxy is None:
            return "Innate proxy not configured (INNATE_SERVICE_KEY)", SkillResult.FAILURE
        if self.manipulation is None or self.mobility is None:
            return "Manipulation/mobility interface not available", SkillResult.FAILURE

        self._ensure_debug_io()
        self._dbg("run_start", prompt=prompt, params=dict(self._p))

        holding = False
        try:
            self.head.set_position(int(round(self._p["tilt_deg"])))
            time.sleep(0.5)
            # Fold the arm to rest BEFORE searching to not hide the camera pov
            try:
                arm_rest_position()
            except SkillFailed as e:
                self.logger.warning(f"[PickAnyObject] rest-before-search failed: {e}")

            self.say(f"Looking for {prompt}.")
            xy = self._search(prompt)
            if xy is None:
                return (
                    f"Could not find '{prompt}' on the floor, even after scanning",
                    SkillResult.FAILURE,
                )

            if self._cancelled:
                return "Cancelled", SkillResult.CANCELLED
            xy = self._position_above(prompt, xy)
            if xy is None:
                return (
                    f"Could not centre '{prompt}' in the pick box",
                    SkillResult.FAILURE,
                )
            self.say("Picking it up.")
            self._grasp_at(prompt, xy)
            if self._grasp_verified(prompt):
                holding = True
                self.say("Got it.")
                return (
                    f"Picked up '{prompt}' (verified: floor clear after backing up)",
                    SkillResult.SUCCESS,
                )
            self.say("I couldn't get a grip on it.")
            return (
                f"Grasp missed — '{prompt}' is still on the floor (verified after "
                "backing up)",
                SkillResult.FAILURE,
            )
        except armlib.ArmUnhealthy as e:
            self.say("My arm isn't responding properly, stopping.")
            return f"Arm servo failure: {e}", SkillResult.FAILURE
        finally:
            self._dbg("run_end")
            self._stop_base()
            try:
                self._rest_arm(keep_grip=holding)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"[PickAnyObject] rest-arm failed: {e}")
            if self.head is not None:
                self.head.set_position(0)

    def cancel(self):
        self._cancelled = True
        if self.mobility is not None:
            self.mobility.send_cmd_vel(0.0, 0.0, 0.1)
        return "Pick cancelled"

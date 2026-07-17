#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pick an object from the floor by text prompt.

Metric localize (pixel -> floor -> base_link), drive into a pick box,
grasp (optional wrist visual-servo), verify by backing up.
No depth camera — URDF + pinhole model.
"""

import json
import math
import time

from innate.skills import SkillCancelled, SkillFailed, arm_rest_position
from std_msgs.msg import String

from brain_client.skills.types import (
    Interface,
    InterfaceType,
    RobotState,
    RobotStateType,
    Skill,
    SkillResult,
)
from workspace.skill_lib import arm as armlib
from workspace.skill_lib import gemini as gemlib
from workspace.skill_lib import vision
from workspace.skill_lib.geometry import IMG_H, IMG_W, floor_to_pixel, pixel_to_floor

# Hardware constants (not live-tunable).
GRIPPER_EMPTY_J6 = -0.085  # open/empty gripper
VERIFY_BACKUP_M = 0.15

# Post-pick carry pose (j1-5). j6 comes from close_strength, not this pose.
CARRY_ARM = [0.0537, -0.5031, 0.4157, 0.9434, -0.0077]

# Live knobs, overridable mid-run via TUNING_TOPIC (String JSON partial dict).
# The webapp overlay (pickOverlay.js) draws the pick/wrist boxes from the
# params/box fields on run_start/params debug events — nothing mirrored there.
TUNABLE = {
    # FIND / LOCALIZE
    "tilt_deg": -20.0,  # head pitch (negative = down)
    "settle_s": 1.2,  # settle before a frame
    # POSITION
    "sweet_x": 0.30,  # pick-box range in base_link (m)
    "box_y": 0.0,  # pick-box lateral offset (m)
    "box_half_px": 40.0,  # outer box half-width (px)
    "accept_frac": 0.5,  # inner accept box as fraction of outer
    "box_steps": 6.0,  # max follow / re-detect attempts
    "bearing_go_deg": 4.0,  # stepwise: turn above this
    "follow_gain_ang": 0.3,  # rad/s per 100 px
    "follow_gain_lin": 0.06,  # m/s per 100 px
    # base odometry
    "rot_tol_deg": 2.5,
    "rot_kp": 1.2,
    "rot_wz_max": 0.5,
    "rot_wz_min": 0.15,  # below this motors don't move
    "drive_tol_m": 0.015,
    "drive_kp": 0.3,
    "drive_v_max": 0.10,
    "drive_v_min": 0.04,
    # WRIST ALIGN (0 wrist_steps = blind grasp)
    "wrist_steps": 2.0,  # Gemini looks (seed + re-seeds)
    "wrist_stop_z": 0.04,  # hand off to blind ladder
    "wrist_z_step": 0.01,
    "wrist_move_s": 0.5,
    "wrist_pitch": 0.82,  # match WRIST_SEARCH_ARM camera pitch
    "wrist_box_u": 320.0,  # wrist goal pixel
    "wrist_box_v": 240.0,
    "wrist_half_px": 60.0,
    "wrist_kx": -0.04,  # m/100px v-error (signed; flip if diverges)
    "wrist_ky": -0.04,  # m/100px u-error; gains at z=0.15, scale w/ height
    "wrist_step_max": 0.04,
    "wrist_settle_s": 0.8,
    # GRASP
    "grasp_x_off": 0.03,  # fingertips ahead of ee_link
    "hover_z": 0.15,
    "hover_s": 2.0,
    "descend_z1": 0.10,
    "descend_z2": 0.06,
    "descend_z3": 0.03,
    "floor_z": 0.01,
    "descend_s": 1.2,
    "descend_abort_z": 0.12,  # EE still above this => limp, abort
    "arm_pitch": 1.30,
    "close_strength": 0.60,  # >~0.6 overcurrent-trips the gripper servo
    "close_s": 1.5,
    "close_settle_s": 0.8,
    "twist_rad": 0.6,  # wind fabric onto fingers
    "lift_rad": 0.6,
}

DEBUG_TOPIC = "/pick_any_object/debug"
TUNING_TOPIC = "/pick_any_object/tuning"

FOLLOW_TIMEOUT_S = 20.0
WRIST_ALIGN_TIMEOUT_S = 60.0
WRIST_MAX_JUMP_PX = 80.0
WRIST_SEG_MIN_SCORE = 25.0
WRIST_CAM_ABOVE_EE = 0.07
WRIST_SEARCH_ARM = [0.1473, -0.0706, -0.4449, 1.3376, -0.0491]


def _round_floats(v, nd=4):
    """Round floats in nested JSON-ish data for telemetry."""
    if isinstance(v, float):
        return round(v, nd)
    if isinstance(v, dict):
        return {k: _round_floats(x, nd) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_round_floats(x, nd) for x in v]
    return v


def _inside_box(px, cu, cv, half):
    """True if pixel is within ±half of (cu, cv)."""
    return abs(px[0] - cu) <= half and abs(px[1] - cv) <= half


def _servo_vel(err_px, gain, v_min, v_max, deadband_px):
    """Pixel P-servo axis: gain per 100 px, clamp to [v_min, v_max], 0 in deadband."""
    if abs(err_px) <= deadband_px:
        return 0.0
    v = max(-v_max, min(v_max, -gain / 100.0 * err_px))
    return math.copysign(v_min, v) if abs(v) < v_min else v


class _BlobTracker:
    """CamShift color-blob tracker seeded from a Gemini box."""

    def __init__(self, hsv, box, px):
        self.model = vision.seg_model(hsv, box)
        self.window = box
        self.guess = px
        self.misses = 0

    @property
    def ok(self):
        return self.model is not None

    def update(self, hsv):
        """Blob center, or None on miss (keeps last window for retry)."""
        pt, window, _score = vision.seg_track(hsv, self.model, self.window, min_score=WRIST_SEG_MIN_SCORE)
        if pt is not None and math.hypot(pt[0] - self.guess[0], pt[1] - self.guess[1]) > WRIST_MAX_JUMP_PX:
            pt = None
        if pt is None:
            self.misses += 1
            return None
        self.misses = 0
        self.window = window
        self.guess = pt
        return pt


class PickAnyObject(Skill):
    """Pick up a floor object described by a natural-language prompt."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    mobility = Interface(InterfaceType.MOBILITY)
    head = Interface(InterfaceType.HEAD)
    main_image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)
    wrist_image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)
    joint_states = RobotState(RobotStateType.LAST_JOINT_STATES)
    odom = RobotState(RobotStateType.LAST_ODOM)

    def __init__(self, logger):
        super().__init__(logger)
        self._proxy = gemlib.make_client()  # None if no credentials
        self._p = dict(TUNABLE)  # live copy; panel mutates
        self._dbg_pub = None
        self._tuning_sub = None

    @property
    def name(self):
        return "pick_any_object"

    def guidelines(self):
        self._ensure_debug_io()  # node ready here; not in __init__
        return (
            "Pick up an object lying on the floor, described in natural language "
            "(e.g. prompt='the white sock', 'a red cup'). The robot localizes the "
            "object metrically with the head camera, drives above it, grasps, and "
            "verifies the grasp by backing up and checking the floor. The arm is "
            "returned to rest either way."
        )

    def _ensure_debug_io(self):
        if self._dbg_pub is not None or self.node is None:
            return
        self._dbg_pub = self.node.create_publisher(String, DEBUG_TOPIC, 10)
        self._tuning_sub = self.node.create_subscription(String, TUNING_TOPIC, self._on_tuning, 10)

    def _on_tuning(self, msg):
        try:
            incoming = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        if not isinstance(incoming, dict):
            return
        applied = {k: float(v) for k, v in incoming.items() if k in self._p and isinstance(v, (int, float))}
        if applied:
            self._p.update(applied)
            self.logger.info(f"[PickAnyObject] tuning applied: {applied}")
        self._dbg("params", params=dict(self._p), box=self._box_px())

    def _dbg(self, ev, **fields):
        if self._dbg_pub is None:
            return
        fields.update(t=round(time.time(), 3), ev=ev)
        self._dbg_pub.publish(String(data=json.dumps(_round_floats(fields))))

    def _checkpoint(self):
        """Raise out of the run if cancel() latched. One raise here, one
        `except SkillCancelled` in execute — no cancelled-as-None plumbing.
        Deliberately NOT called during close/twist/lift: once the fingers
        commit, aborting mid-grip would drag the object on the way home."""
        if self._cancelled:
            raise SkillCancelled("Pick cancelled")

    def _gemini_call(self, image_b64, question):
        """Vision Q&A via gemlib."""
        return gemlib.ask_image(self._proxy, image_b64, question, logger=self.logger)

    def _detect_px(self, prompt):
        """Head frame -> best grasp pixel, or None."""
        self._stop_base()
        time.sleep(self._p["settle_s"])
        img = self.main_image
        if not img:
            return None
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
        return vision.parse_det_px(text)

    def _localize_px(self, prompt):
        """Detect + back-project -> ((x,y)|None, pixel|None)."""
        px = self._detect_px(prompt)
        if px is None:
            self._dbg("localize", px=None, xy=None)
            return None, None
        xy = pixel_to_floor(px[0], px[1], self._p["tilt_deg"])
        if xy:
            self.logger.info(f"[PickAnyObject] px=({px[0]:.0f},{px[1]:.0f}) -> base_link ({xy[0]:.3f},{xy[1]:.3f})")
        self._dbg("localize", px=px, xy=xy, tilt_deg=self._p["tilt_deg"])
        return xy, px

    def _stop_base(self):
        self.mobility.send_cmd_vel(0.0, 0.0, 0.1)

    def _odom_xyt(self):
        """(x, y, theta) from nested dict or flat dataclass."""
        od = self.odom
        if od is None:
            return None
        try:
            if isinstance(od, dict):
                p = od["pose"]["pose"]["position"]
                return (float(p["x"]), float(p["y"]), math.radians(float(od["theta_degrees"])))
            return (float(od.x), float(od.y), float(od.theta))
        except (KeyError, AttributeError, TypeError):
            return None

    def _rotate_by(self, angle, tol=None, timeout=12.0):
        self._dbg("rotate", angle_deg=math.degrees(angle))
        od = self._odom_xyt()
        if od is None:
            self.mobility.send_cmd_vel(0.0, math.copysign(0.35, angle), abs(angle) / 0.35)
            time.sleep(abs(angle) / 0.35 + 0.4)
            return
        target = od[2] + angle
        t0 = time.time()
        err = angle
        while time.time() - t0 < timeout:
            self._checkpoint()
            od = self._odom_xyt()
            if od is None:
                break
            err = math.atan2(math.sin(target - od[2]), math.cos(target - od[2]))
            if abs(err) < (tol if tol is not None else math.radians(self._p["rot_tol_deg"])):
                break
            wz_max = self._p["rot_wz_max"]
            wz = max(-wz_max, min(wz_max, self._p["rot_kp"] * err))
            if abs(wz) < self._p["rot_wz_min"]:
                wz = math.copysign(self._p["rot_wz_min"], wz)
            self.mobility.send_cmd_vel(0.0, wz, 0.15)
            time.sleep(0.08)
        self._stop_base()
        self._dbg("rotate_done", err_deg=math.degrees(err), s=time.time() - t0)

    def _drive(self, dist, tol=None, timeout=15.0):
        tol = tol if tol is not None else self._p["drive_tol_m"]
        if abs(dist) < tol:
            return
        self._dbg("drive", dist_m=dist)
        od = self._odom_xyt()
        if od is None:
            self.logger.warning("[PickAnyObject] no odom — open-loop drive")
            self.mobility.send_cmd_vel(math.copysign(0.08, dist), 0.0, abs(dist) / 0.08)
            time.sleep(abs(dist) / 0.08 + 0.4)
            return
        x0, y0 = od[0], od[1]
        t0 = time.time()
        err = abs(dist)
        while time.time() - t0 < timeout:
            self._checkpoint()
            od = self._odom_xyt()
            if od is None:
                break
            gone = math.hypot(od[0] - x0, od[1] - y0)
            err = abs(dist) - gone
            if err < tol:
                break
            v = math.copysign(
                max(self._p["drive_v_min"], min(self._p["drive_v_max"], self._p["drive_kp"] * err)),
                dist,
            )
            self.mobility.send_cmd_vel(v, 0.0, 0.15)
            time.sleep(0.08)
        self._stop_base()
        self._dbg("drive_done", err_m=err, s=time.time() - t0)

    def _ee_xyz(self):
        return armlib.ee_xyz(self.manipulation)

    def _gripper_j6(self):
        return armlib.gripper_j6(self.joint_states)

    def _rest_arm(self, keep_grip):
        """Fold home, or CARRY_ARM with full close_strength if holding."""
        if keep_grip:
            target = CARRY_ARM + [-self._p["close_strength"]]
        else:
            target = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        for _ in range(2):
            self.manipulation.move_to_joint_positions(
                joint_positions=target,
                duration=3,
                blocking=True,
            )
            time.sleep(0.3)

    def _search(self, prompt):
        """Scan: straight, right 30°, left 60°. First hit wins. (+yaw=left)"""
        for i, turn in enumerate((0.0, -math.radians(30), math.radians(60))):
            self._checkpoint()
            if turn:
                if i == 1:
                    self.say("Scanning around for it.")
                self._rotate_by(turn)
            xy, _px = self._localize_px(prompt)
            if xy is not None:
                return xy
        return None

    def _box_px(self):
        """[cu, cv, half, accept] pick box in image px, or None off-image.
        Sent on run_start/params events; the webapp overlay draws this box
        instead of mirroring the aim knobs and camera model in JS."""
        c = floor_to_pixel(self._p["sweet_x"], self._p["box_y"], self._p["tilt_deg"])
        if c is None:
            return None
        half = self._p["box_half_px"]
        return [c[0], c[1], half, half * self._p["accept_frac"]]

    def _sweet_box(self):
        """(center_px, outer_half, accept_half). Stop only inside accept."""
        box = self._box_px()
        if box is None:  # reachable via live tuning; assert would vanish under -O
            raise RuntimeError("pick box off-image — check tilt_deg/sweet_x")
        cu, cv, half, accept = box
        return (cu, cv), half, accept

    def _gray_frame(self):
        img = self.main_image
        return vision.b64_to_gray(img) if img else None

    def _follow_into_box(self, seed_px):
        """Optical-flow base servo into pick box. No Gemini.
        Returns ('in_box'|'lost'|'timeout'|'noframe', px|None).
        """
        prev = self._gray_frame()
        if prev is None:
            return "noframe", None
        u, v = seed_px
        grid = vision.grid_pts(u, v)
        in_box, last_dbg = 0, 0.0
        t0 = time.time()
        while time.time() - t0 < FOLLOW_TIMEOUT_S:
            self._checkpoint()
            gray = self._gray_frame()
            if gray is None:
                time.sleep(0.03)
                continue
            tracked = vision.track_point(prev, gray, grid)
            prev = gray
            if tracked is None:
                self._stop_base()
                return "lost", None
            u, v = tracked
            grid = vision.grid_pts(u, v)
            if not (0 <= u < IMG_W and 0 <= v < IMG_H):
                self._stop_base()
                return "lost", None

            (cu, cv), half, accept = self._sweet_box()
            inside = _inside_box((u, v), cu, cv, accept)

            now = time.time()
            if now - last_dbg > 0.1:
                self._dbg("servo", px=[u, v], box=[cu, cv, half, accept], inside=inside)
                last_dbg = now

            if inside:
                in_box += 1
                self._stop_base()
                if in_box >= 3:
                    return "in_box", (u, v)
                time.sleep(0.03)
                continue
            in_box = 0

            # Deadband = accept (inner) box; right -> -wz, too close (low) -> -vx.
            wz = _servo_vel(u - cu, self._p["follow_gain_ang"], self._p["rot_wz_min"], self._p["rot_wz_max"], accept)
            vx = _servo_vel(v - cv, self._p["follow_gain_lin"], self._p["drive_v_min"], self._p["drive_v_max"], accept)
            self.mobility.send_cmd_vel(vx, wz, 0.15)
            time.sleep(0.03)
        self._stop_base()
        return "timeout", None

    def _position_above(self, prompt, xy):
        """Flow-follow into pick box; Gemini reseed/confirm. Stepwise if no cam."""
        if self._gray_frame() is None:
            return self._position_stepwise(prompt, xy)

        seed = floor_to_pixel(xy[0], xy[1], self._p["tilt_deg"])
        for attempt in range(int(self._p["box_steps"])):
            self._checkpoint()
            if seed is None:
                seed = self._detect_px(prompt)
                if seed is None:
                    self._dbg("position_done", xy=None, reason="lost")
                    return None
            result, _pt = self._follow_into_box(seed)
            if result == "noframe":
                return self._position_stepwise(prompt, xy)
            if result == "lost":
                seed = None
                continue
            xy2, px2 = self._localize_px(prompt)
            if px2 is None:
                self._dbg("position_done", xy=None, reason="lost")
                return None
            (cu, cv), _half, accept = self._sweet_box()
            if xy2 is not None and _inside_box(px2, cu, cv, accept):
                self._dbg("position_done", xy=xy2, attempts=attempt)
                return xy2
            seed = px2 if xy2 is not None else None
        self._dbg("position_done", xy=None, reason="never inside the box")
        return None

    def _position_stepwise(self, prompt, xy):
        """No-camera fallback: turn OR drive, re-detect, repeat."""
        target_bearing = math.atan2(self._p["box_y"], self._p["sweet_x"])
        target_range = math.hypot(self._p["sweet_x"], self._p["box_y"])
        px = floor_to_pixel(xy[0], xy[1], self._p["tilt_deg"])
        for step in range(int(self._p["box_steps"])):
            self._checkpoint()
            if px is None:
                xy, px = self._localize_px(prompt)
                if px is None:
                    self._dbg("position_done", xy=None, reason="lost")
                    return None
            (cu, cv), half, accept = self._sweet_box()
            inside = xy is not None and _inside_box(px, cu, cv, accept)
            self._dbg("position", step=step, px=px, xy=xy, box=[cu, cv, half, accept], inside=inside)
            if inside:
                self._dbg("position_done", xy=xy, steps=step)
                return xy
            if xy is None:
                px = None
                continue
            bearing_err = math.atan2(xy[1], xy[0]) - target_bearing
            if abs(bearing_err) > math.radians(self._p["bearing_go_deg"]):
                self._rotate_by(bearing_err)
            else:
                self._drive(math.hypot(xy[0], xy[1]) - target_range)
            px = None
        self._dbg("position_done", xy=None, reason="never inside the box")
        return None

    # GRASP: search pose -> seed -> servo down -> blind push -> close/twist/lift
    # (wrist_steps=0 skips to blind hover + push)
    def _wrist_seed(self, prompt, z):
        """Wrist Gemini box -> (center_px, box) or (None, None)."""
        time.sleep(self._p["wrist_settle_s"])
        img = self.wrist_image
        text = (
            self._gemini_call(
                img,
                f"Wrist camera on a robot gripper, looking down at the floor. "
                f"Find '{prompt}' on the floor. Ignore the gripper fingers "
                "themselves. Return ONLY a JSON list of matches, each "
                '{"box_2d":[ymin,xmin,ymax,xmax]} normalized 0-1000, best first, '
                "each box TIGHT around its object. Empty list if not visible.",
            )
            if img
            else None
        )
        box = vision.parse_det_box(text)
        px = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0) if box else None
        self._dbg("wrist_seed", z=z, px=px)
        return px, box

    def _next_wrist_hsv(self, last_b64, timeout=1.5):
        """Wait for a new wrist frame -> (hsv|None, b64)."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            self._checkpoint()
            img = self.wrist_image
            if img and img != last_b64:
                hsv = vision.b64_to_hsv(img)
                if hsv is not None:
                    return hsv, img
            time.sleep(0.04)
        return None, last_b64

    def _wrist_done(self, x, y, z, reason):
        """Telemetry + return value for every _wrist_servo exit."""
        grab = floor_to_pixel(x + self._p["grasp_x_off"], y, self._p["tilt_deg"])
        self._dbg("wrist_done", tx=x, ty=y, z=z, reason=reason, grab_px=grab)
        return x, y, z

    def _wrist_reseed(self, prompt, z, raw):
        """Persistent tracking loss: one Gemini look + fresh color model
        (the view changed). Returns (tracker, raw, fail_reason) — tracker
        is None on failure, fail_reason "" on success."""
        px, box = self._wrist_seed(prompt, z)
        if px is None:
            return None, raw, "lost track"
        hsv, raw = self._next_wrist_hsv(raw)
        if hsv is None:
            return None, raw, "no wrist frames"
        tracker = _BlobTracker(hsv, box, px)
        if not tracker.ok:
            return None, raw, "lost track"
        return tracker, raw, ""

    def _wrist_servo(self, prompt, tx, ty):
        """Wrist CamShift servo: watch until 2 frames agree (arm still),
        then act — one nudge toward the wrist box, or one wrist_z_step down
        once it has been seen inside the box twice; repeat to wrist_stop_z.
        A miss gets 2 frames of patience, then a Gemini re-seed (budget =
        wrist_steps - 1). Color model, not LK: the object grows/deforms
        during the descent and optical flow slides off.
        Returns (x,y,z); falls back to (tx,ty) if never seen."""
        p = self._p
        ee = self._ee_xyz()
        z = ee[2] if ee else p["hover_z"]
        looks = int(p["wrist_steps"]) - 1

        px, box = self._wrist_seed(prompt, z)
        if px is None:
            return self._wrist_done(tx, ty, z, "not seen")
        x, y = (ee[0], ee[1]) if ee else (tx, ty)  # servo from the real pose

        hsv, raw = self._next_wrist_hsv(None)
        if hsv is None:
            return self._wrist_done(tx, ty, z, "no wrist frames")
        tracker = _BlobTracker(hsv, box, px)
        if not tracker.ok:
            return self._wrist_done(tx, ty, z, "not seen")

        deadline = time.time() + WRIST_ALIGN_TIMEOUT_S
        streak = 0  # verified matches since the arm last moved
        centered = 0  # consecutive matches INSIDE the box
        reason = "reached stop z"
        while z > p["wrist_stop_z"] + 1e-6:
            self._checkpoint()
            if time.time() > deadline:
                reason = "timeout"
                break
            hsv, raw = self._next_wrist_hsv(raw)
            if hsv is None:
                reason = "no wrist frames"
                break

            px = tracker.update(hsv)
            if px is None:
                streak = centered = 0
                if tracker.misses < 3:
                    continue  # transient (blur / mid-move frame) — wait
                if looks <= 0:
                    reason = "lost track"
                    break
                looks -= 1
                tracker, raw, fail = self._wrist_reseed(prompt, z, raw)
                if tracker is None:
                    reason = fail
                    break
                px = tracker.guess  # the re-seed detection is this frame's fix
            streak += 1

            err_u = px[0] - p["wrist_box_u"]
            err_v = px[1] - p["wrist_box_v"]
            inside = _inside_box(px, p["wrist_box_u"], p["wrist_box_v"], p["wrist_half_px"])
            centered = centered + 1 if inside else 0
            self._dbg("wrist_servo", px=px, z=z, inside=inside, tx=x, ty=y, ee=self._ee_xyz())
            if streak < 2:
                continue  # watch one more frame before trusting it
            if inside and centered < 2:
                continue  # just entered the box — confirm it stays

            if centered >= 2:
                z = max(p["wrist_stop_z"], z - p["wrist_z_step"])
                # descent barely moves a centered pixel: guess stays px
            else:
                # Gains tuned at z=0.15; scale with camera height.
                s = (z + WRIST_CAM_ABOVE_EE) / (0.15 + WRIST_CAM_ABOVE_EE)
                cap = p["wrist_step_max"]
                x += max(-cap, min(cap, p["wrist_kx"] / 100.0 * err_v * s))
                y += max(-cap, min(cap, p["wrist_ky"] / 100.0 * err_u * s))
                x, y = armlib.clamp_reach(x, y)
                # feed-forward: expect the blob at the box center next frame
                tracker.guess = (p["wrist_box_u"], p["wrist_box_v"])
            armlib.move_checked(
                self.manipulation, x, y, z, pitch=p["wrist_pitch"], duration=p["wrist_move_s"], logger=self.logger
            )
            streak = 0
            centered = 0  # view shifted — re-confirm centering

        return self._wrist_done(x, y, z, reason)

    def _goto_search_pose(self, bearing):
        """WRIST_SEARCH_ARM aimed at bearing; pins IK to elbow-up branch."""
        a = WRIST_SEARCH_ARM
        j6 = self._gripper_j6()
        pose = [bearing, a[1], a[2], self._p["wrist_pitch"] - a[1] - a[2], a[4], j6 if j6 is not None else 0.0]
        self.manipulation.move_to_joint_positions(joint_positions=pose, duration=self._p["hover_s"], blocking=True)
        time.sleep(0.3)
        self._dbg("hover", ee=self._ee_xyz())

    def _open_gripper_checked(self):
        """Open gripper with trip recovery."""
        armlib.open_checked(
            self.manipulation,
            self._gripper_j6,
            logger=self.logger,
            on_reboot=lambda j6: self._dbg("gripper_reboot", j6=j6),
        )

    def _push_to_floor(self, x, y, z_from, deep):
        """Blind descent ladder to floor. Failed rung = contact; abort if still high."""
        p = self._p
        rungs = [p["descend_z1"], p["descend_z2"], p["descend_z3"], 0.0 if deep else p["floor_z"]]
        for z in rungs:
            if z >= z_from - 1e-6:
                continue
            self._checkpoint()
            ok = self.manipulation.move_to_cartesian_pose(
                x=x,
                y=y,
                z=z,
                roll=0.0,
                pitch=p["arm_pitch"],
                yaw=0.0,
                duration=p["descend_s"],
                blocking=True,
            )
            self._dbg("descend", z=z, ok=bool(ok), ee=self._ee_xyz())
            if not ok:
                armlib.recover(self.manipulation, self.logger)
                break
        ee = self._ee_xyz()
        if ee is not None and ee[2] > p["descend_abort_z"]:
            self._dbg("grasp_abort", reason="arm would not descend", ee_z=ee[2])
            armlib.recover(self.manipulation, self.logger)
            raise armlib.ArmUnhealthy("arm would not descend")

    def _close_twist_lift(self, x, y):
        """Close, joint-space twist+lift (IK would unwind j5). Hold full close cmd."""
        p = self._p
        j6_open = self._gripper_j6()
        armlib.close(self.manipulation, strength=p["close_strength"], duration=p["close_s"])
        time.sleep(p["close_settle_s"])
        self._dbg("close", j6_open=j6_open, j6=self._gripper_j6(), strength=p["close_strength"])

        grip = -p["close_strength"]
        try:
            j = list(self.joint_states["position"][:6])
            j[4] = max(-1.4, min(1.4, j[4] + p["twist_rad"]))
            j[5] = grip
            self.manipulation.move_to_joint_positions(joint_positions=j, duration=1.0, blocking=True)
            time.sleep(0.3)
            self._dbg("twist", j4=j[4])
            j = list(self.joint_states["position"][:6])
            j[1] = max(-1.4, j[1] - p["lift_rad"])
            j[5] = grip
            self.manipulation.move_to_joint_positions(joint_positions=j, duration=2.0, blocking=True)
            time.sleep(0.3)
            self._dbg("lift", ee=self._ee_xyz(), j6=self._gripper_j6())
        except (KeyError, IndexError, TypeError):
            armlib.move_checked(
                self.manipulation, x, y, 0.22, pitch=p["arm_pitch"], duration=2.0, tol=0.10, logger=self.logger
            )
            self._dbg("lift", ee=self._ee_xyz(), j6=self._gripper_j6(), ik_fallback=True)

    def _grasp_at(self, prompt, xy, deep=False):
        """Full grasp at floor xy (base_link). Reads live self._p knobs."""
        p = self._p
        x, y = armlib.clamp_reach(xy[0] - p["grasp_x_off"], xy[1])

        grab_px = floor_to_pixel(x + p["grasp_x_off"], y, p["tilt_deg"])
        obj_px = floor_to_pixel(xy[0], xy[1], p["tilt_deg"])
        clamped = abs((xy[0] - p["grasp_x_off"]) - x) > 1e-4 or abs(xy[1] - y) > 1e-4
        self._dbg("grasp", xy=xy, tx=x, ty=y, deep=deep, grab_px=grab_px, obj_px=obj_px, clamped=clamped)

        self._open_gripper_checked()

        if p["wrist_steps"] >= 1:
            self._goto_search_pose(math.atan2(y, x))
            x, y, z = self._wrist_servo(prompt, x, y)
        else:
            z = p["hover_z"]
            armlib.move_checked(
                self.manipulation, x, y, z, pitch=p["arm_pitch"], duration=p["hover_s"], logger=self.logger
            )
            self._dbg("hover", ee=self._ee_xyz())

        self._push_to_floor(x, y, z, deep)
        self._checkpoint()  # last exit before the fingers commit
        self._close_twist_lift(x, y)

    def _grasp_verified(self, prompt):
        """Back up, then check floor clear + gripper not open. Gemini gets
        BOTH cameras: the head view answers "is it still on the floor?", the
        wrist view (mirrored) can show the object in the fingers so a held
        object isn't mistaken for a dropped one."""
        self._drive(-VERIFY_BACKUP_M)
        time.sleep(self._p["settle_s"])
        j6 = self._gripper_j6()
        images = [img for img in (self.main_image, self.wrist_image) if img]
        wrist_note = (
            " Image 2 is the WRIST camera next to the gripper fingers "
            "(mirrored) — the object may be visible held in the fingers there."
            if len(images) > 1
            else ""
        )
        floor_text = (
            self._gemini_call(
                images,
                f"Robot just tried to pick up '{prompt}'. Image 1 is the head "
                f"camera looking at the floor.{wrist_note} "
                f"Is '{prompt}' lying ON the floor/carpet anywhere in view? "
                "Something hanging from the robot's gripper or held between the "
                "gripper fingers does NOT count as on the floor. "
                "Answer only YES or NO.",
            )
            if images
            else None
        )
        floor_clear = bool(floor_text) and "NO" in floor_text.upper()
        j6_ok = j6 is not None and j6 > GRIPPER_EMPTY_J6 + 0.02
        held = floor_clear and j6_ok
        self.logger.info(
            f"[PickAnyObject] verify: floor={floor_text!r} j6={j6} "
            f"({len(images)} cams) -> {'HELD' if held else 'NOT HELD'}"
        )
        self._dbg("verify", floor_clear=floor_clear, j6=j6, held=held, cams=len(images))
        return held

    def execute(self, prompt: str = "the sock", max_base_steps: int = 10):
        """Pick up `prompt` from the floor. max_base_steps unused (compat)."""
        if self._proxy is None:
            return "Innate proxy not configured (INNATE_SERVICE_KEY)", SkillResult.FAILURE
        if self.manipulation is None or self.mobility is None or self.head is None:
            return "Manipulation/mobility/head interface not available", SkillResult.FAILURE

        self._ensure_debug_io()
        self._dbg("run_start", prompt=prompt, params=dict(self._p), box=self._box_px())

        holding = False
        try:
            self.head.set_position(int(round(self._p["tilt_deg"])))
            time.sleep(0.5)
            # Rest arm first so it doesn't occlude the head camera.
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
                f"Grasp missed — '{prompt}' is still on the floor (verified after backing up)",
                SkillResult.FAILURE,
            )
        except SkillCancelled:
            return "Pick cancelled", SkillResult.CANCELLED
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
            self.head.set_position(0)  # non-None: guarded at entry

    def cancel(self):
        self._cancelled = True
        if self.mobility is not None:
            self.mobility.send_cmd_vel(0.0, 0.0, 0.1)
        return "Pick cancelled"

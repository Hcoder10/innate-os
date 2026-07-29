#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pick an object from the floor by text prompt.

Metric localize (pixel -> floor -> base_link), drive into a pick box,
grasp (optional wrist visual-servo), verify by backing up.
No depth camera — URDF + pinhole model.
"""

import math
import re
import time

from innate_skills.arm_rest_position import ArmRestPosition
from innate_skills.gripper_close import GripperClose
from innate_skills.gripper_open import GripperOpen

from brain_client.robot.manipulation import ArmCancelled, ArmFailed, ArmUnhealthy
from innate import (
    Head,
    JointStates,
    MainImage,
    Manipulation,
    Mobility,
    Odometry,
    Skill,
    SkillCancelled,
    SkillFailed,
    SkillResult,
    SkillReturn,
    WristImage,
    resource,
    vision,
)
from innate import gemini as gemlib
from innate.geometry import IMG_H, IMG_W, floor_to_pixel, pixel_to_floor

# Hardware constants (not live-tunable).
GRIPPER_EMPTY_J6 = -0.085  # open/empty gripper
VERIFY_BACKUP_M = 0.15
# Clamp for Gemini's per-object grip_strength (see the _detect_px prompt).
# Since the gripper moved to a hardware current cap, this no longer sets any
# force — it only serves as the soft/rigid signal for the fabric twist.
GRIP_STRENGTH_RANGE = (0.30, 0.60)

# Gemini grip_strength at/above this = fabric-like: gets the fabric twist.
SOFT_GRIP_MIN = 0.5

# Post-pick carry pose (j1-5). j6 comes from close_strength, not this pose.
# j2 = -0.50, not the recorded -0.5031: the coupling limit at j1~0.05 clamps
# to -0.500 and anything past it makes the arm node log the clamp forever.
CARRY_ARM = [0.0537, -0.50, 0.4157, 0.9434, -0.0077]

# Pick parameters, tuned on hardware. Fixed at load.
PARAMS = {
    # FIND / LOCALIZE
    "tilt_deg": -20.0,  # head pitch (negative = down); head servo range is -40..70 (HEAD_MIN/MAX_DEG)
    "settle_s": 1.2,  # settle before a frame
    # POSITION
    "sweet_x": 0.37,  # pick-box range in base_link (m) — the
    #   base parks the object this far ahead, so it also
    #   sets how far forward the grasp reaches
    #   (sweet_x - grasp_x_off) and how high the box sits
    #   on the image (~5 px per cm at tilt_deg=-20). Hard
    #   ceiling is 0.43: past that the grasp target
    #   exceeds REACH_X and the reach clamp lands the
    #   fingers short of the object. Kept back from
    #   that edge: grasping at the 0.40 reach ceiling
    #   stalls the wrist servo and over-torques the
    #   shoulder (servo 2 overload); 0.37 grasps at ~0.34.
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
    "wrist_stop_z": 0.05,  # hand off to blind ladder (was 0.04; leave a bit more air)
    "wrist_z_step": 0.01,
    "wrist_move_s": 0.5,
    "wrist_pitch": 0.82,  # match WRIST_SEARCH_ARM camera pitch
    "wrist_box_u": 320.0,  # wrist goal pixel
    # Below image center (240): the wrist cam sits above the fingertips, so
    # mid-frame aims short of them. 380 is the hardware-tuned parallax bias
    # (started at 300; hardware runs kept landing behind the object).
    "wrist_box_v": 380.0,
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
    "descend_z2": 0.07,
    "descend_z3": 0.045,
    # ee_link target — not fingertip height. 0.01 dug into carpet: the
    # cartesian rung fails, recover() reboots the arm, and the grasp aborts.
    "floor_z": 0.03,
    "descend_s": 1.2,
    "descend_abort_z": 0.12,  # EE still above this => limp, abort
    "arm_pitch": 1.30,
    # Grip force lives in HARDWARE now: servo 6 runs current-based position
    # control (arm_config.yaml control_mode 5, current_limit in mA), so a
    # deep close command squeezes any object at that constant, safe force —
    # the position error no longer sets the force and cannot overload the
    # servo. close_strength is just the close depth (how far the command
    # goes past any object; also the empty-close depth). Note gripper_close.py
    # puts the hardware ceiling at 0.6: above that the servo trips and needs
    # a reboot to clear.
    "close_strength": 0.60,
    "close_s": 1.5,
    "close_settle_s": 0.8,
    "close_lift_m": 0.01,  # un-press before closing: the
    #   descent parks the fingers pressed into the floor; a small
    #   lift lets them close around the object, not drag it
    "twist_rad": 0.6,  # wind fabric onto fingers
    "lift_rad": 0.6,
}


FOLLOW_TIMEOUT_S = 20.0
WRIST_ALIGN_TIMEOUT_S = 60.0
WRIST_MAX_JUMP_PX = 80.0
WRIST_SEG_MIN_SCORE = 25.0
WRIST_CAM_ABOVE_EE = 0.07
WRIST_SEARCH_ARM = [0.1473, -0.0706, -0.4449, 1.3376, -0.0491]


def _inside_box(px, cu, cv, half):
    return abs(px[0] - cu) <= half and abs(px[1] - cv) <= half


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
    """Pick up an object lying on the floor, described in natural language
    (e.g. prompt='the white sock', 'a red cup'). The robot localizes the
    object metrically with the head camera, drives above it, grasps, and
    verifies the grasp by backing up and checking the floor. The arm is
    returned to rest either way."""

    manipulation: Manipulation
    mobility: Mobility
    head: Head
    # ``| None`` — best effort, every read is guarded: positioning falls back
    # to stepwise re-detection without head frames, the wrist stage degrades
    # to the blind grasp without wrist frames, and the interface readers
    # tolerate missing odom/joint states.
    main_image: MainImage | None
    wrist_image: WristImage | None
    joint_states: JointStates | None
    odom: Odometry | None

    # composed sub-skills: the declared class is what runs
    gripper_open: GripperOpen
    gripper_close: GripperClose
    arm_rest: ArmRestPosition

    def __init__(self, logger):
        super().__init__(logger)
        self._p = PARAMS
        self._grip_strength = None  # Gemini's hardness rating; set on detection
        self._holding = False  # fingers committed on an object this run

    @resource
    def _proxy(self):
        # Innate Gemini proxy client; None if no credentials. Lazy: built on
        # the first vision call.
        return gemlib.make_client()

    def _checkpoint(self):
        """Raise out of the run if a cancel latched. Failures raise
        SkillFailed / SkillCancelled and unwind to execute's handlers —
        no None-as-failure plumbing. Deliberately NOT called during
        close/twist/lift: once the fingers commit, aborting mid-grip
        would drag the object on the way home."""
        self.check_cancelled()

    def _detect_px(self, prompt):
        """Head frame -> best grasp pixel, or None. Also records Gemini's
        per-object grip_strength (self._grip_strength) for the close."""
        self._stop_base()
        time.sleep(self._p["settle_s"])
        img = self.main_image
        if not img:
            return None
        text = gemlib.ask_image(
            self._proxy,
            img,
            f"Find '{prompt}' lying on the floor in this image. Match precisely — "
            "not paper/packaging when asked for clothing, and NOT anything held "
            "by the robot arm. Return ONLY a JSON list of matches, each "
            '{"box_2d":[ymin,xmin,ymax,xmax], "grasp_point":[y,x], '
            '"grip_strength":s} normalized 0-1000, best first. grasp_point is '
            "the CENTER of the object (geometric middle of the visible blob), "
            "not an edge or tip. grip_strength is how hard a parallel gripper "
            "should squeeze this object, 0.30-0.60: soft/deformable objects "
            "(socks, fabric, plush) need 0.60 or they slip out; rigid/hard "
            "objects (metal, hard plastic, wood, ceramic) need 0.30-0.40 — "
            "squeezing them harder stalls the gripper servo. "
            "Empty list if not present.",
            logger=self.logger,
            cancelled=lambda: self.cancelled,
        )
        px = vision.parse_det_px(text)
        grip = vision.parse_det_grip(text)
        if px is not None and grip is not None:
            lo, hi = GRIP_STRENGTH_RANGE
            self._grip_strength = max(lo, min(hi, grip))
        return px

    def _localize_px(self, prompt):
        """Detect + back-project -> ((x,y)|None, pixel|None)."""
        px = self._detect_px(prompt)
        if px is None:
            return None, None
        xy = pixel_to_floor(px[0], px[1], self._p["tilt_deg"])
        if xy:
            self.logger.info(f"[PickAnyObject] px=({px[0]:.0f},{px[1]:.0f}) -> base_link ({xy[0]:.3f},{xy[1]:.3f})")
        return xy, px

    def _localize_retry(self, prompt):
        """_localize_px with one retry: a single "not visible" is noise, not
        absence — Gemini can deny an object on one look and match it again
        on the next."""
        xy, px = self._localize_px(prompt)
        if px is None:
            xy, px = self._localize_px(prompt)
        return xy, px

    def _stop_base(self):
        self.mobility.stop()

    def _rotate_by(self, angle):
        self.mobility.rotate_by(
            lambda: self.mobility.odom_xyt(self.odom),
            angle,
            kp=self._p["rot_kp"],
            wz_max=self._p["rot_wz_max"],
            wz_min=self._p["rot_wz_min"],
            tol=math.radians(self._p["rot_tol_deg"]),
            cancelled=lambda: self.cancelled,
            logger=self.logger,
        )
        self._checkpoint()  # rotate_by returns early on cancel — unwind here

    def _drive(self, dist):
        self.mobility.drive(
            lambda: self.mobility.odom_xyt(self.odom),
            dist,
            kp=self._p["drive_kp"],
            v_max=self._p["drive_v_max"],
            v_min=self._p["drive_v_min"],
            tol=self._p["drive_tol_m"],
            cancelled=lambda: self.cancelled,
            logger=self.logger,
        )
        self._checkpoint()  # drive returns early on cancel — unwind here

    def _rest_arm(self, keep_grip):
        """Best-effort teardown: carry if holding, else fold to rest. Never
        raises. REST, not ZERO: after a failed descent the arm can be near the
        floor, and the zero posture would sweep the gripper through it."""
        joints = CARRY_ARM + [-self._p["close_strength"]] if keep_grip else list(self.manipulation.REST)
        try:
            self.manipulation.go(joints, duration=3.0, times=2)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the run result
            self.logger.warning(f"[PickAnyObject] rest-arm failed: {e}")

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
        raise SkillFailed(f"Could not find '{prompt}' on the floor, even after scanning")

    def _sweet_box(self):
        """(center_px, outer_half, accept_half). Stop only inside accept."""
        c = floor_to_pixel(self._p["sweet_x"], self._p["box_y"], self._p["tilt_deg"])
        if c is None or not (0 <= c[0] < IMG_W and 0 <= c[1] < IMG_H):
            # behind the camera plane or projected outside the frame — bad
            # tilt_deg/sweet_x params; assert would vanish under -O
            raise SkillFailed("pick box off-image — check tilt_deg/sweet_x")
        half = self._p["box_half_px"]
        return (c[0], c[1]), half, half * self._p["accept_frac"]

    def _follow_into_box(self, seed_px):
        """Optical-flow base servo into pick box. No Gemini.
        Returns ('in_box'|'lost'|'timeout'|'noframe', px|None).
        """
        raw = self.main_image
        prev = vision.b64_to_gray(raw) if raw else None
        if prev is None:
            return "noframe", None
        u, v = seed_px
        grid = vision.grid_pts(u, v)
        in_box = 0
        t0 = time.time()
        while time.time() - t0 < FOLLOW_TIMEOUT_S:
            self._checkpoint()
            # Only track NEW frames (cf. _next_wrist_hsv): the camera runs
            # slower than this loop, and a stale frame re-decoded and re-tracked
            # is wasted CPU — and would let the in_box streak count one real
            # observation three times.
            img = self.main_image
            if not img or img == raw:
                time.sleep(0.03)
                continue
            gray = vision.b64_to_gray(img)
            raw = img
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

            (cu, cv), _half, accept = self._sweet_box()
            inside = _inside_box((u, v), cu, cv, accept)
            if inside:
                in_box += 1
                self._stop_base()
                if in_box >= 3:
                    return "in_box", (u, v)
                time.sleep(0.03)
                continue
            in_box = 0

            # Deadband = accept (inner) box; right -> -wz, too close (low) -> -vx.
            wz = self.mobility.servo_vel(
                u - cu, self._p["follow_gain_ang"], self._p["rot_wz_min"], self._p["rot_wz_max"], accept
            )
            vx = self.mobility.servo_vel(
                v - cv, self._p["follow_gain_lin"], self._p["drive_v_min"], self._p["drive_v_max"], accept
            )
            self.mobility.send_cmd_vel(vx, wz, 0.15)
            if self.cancelled:
                # the on_cancel brake fired between the loop checkpoint and
                # the send, which re-commanded motion — undo it and unwind
                # now, not at the next checkpoint
                self._stop_base()
                self._checkpoint()
            time.sleep(0.03)
        self._stop_base()
        return "timeout", None

    def _position_failed(self, prompt):
        raise SkillFailed(f"Could not centre '{prompt}' in the pick box")

    def _position_above(self, prompt, xy):
        """Flow-follow into pick box; Gemini reseed/confirm. Stepwise if no cam.
        Raises SkillFailed if the object cannot be centred."""
        if not self.main_image:
            return self._position_stepwise(prompt, xy)

        seed = floor_to_pixel(xy[0], xy[1], self._p["tilt_deg"])
        for _attempt in range(int(self._p["box_steps"])):
            self._checkpoint()
            if seed is None:
                seed = self._detect_px(prompt) or self._detect_px(prompt)  # one miss is noise — retry once
                if seed is None:
                    self._position_failed(prompt)
            result, _pt = self._follow_into_box(seed)
            if result == "noframe":
                return self._position_stepwise(prompt, xy)
            if result == "lost":
                seed = None
                continue
            xy2, px2 = self._localize_retry(prompt)
            if px2 is None:
                self._position_failed(prompt)
            (cu, cv), _half, accept = self._sweet_box()
            if xy2 is not None and _inside_box(px2, cu, cv, accept):
                return xy2
            seed = px2 if xy2 is not None else None
        self._position_failed(prompt)

    def _position_stepwise(self, prompt, xy):
        """No-camera fallback: turn OR drive, re-detect, repeat.
        Raises SkillFailed if the object cannot be centred."""
        target_bearing = math.atan2(self._p["box_y"], self._p["sweet_x"])
        target_range = math.hypot(self._p["sweet_x"], self._p["box_y"])
        px = floor_to_pixel(xy[0], xy[1], self._p["tilt_deg"])
        for _step in range(int(self._p["box_steps"])):
            self._checkpoint()
            if px is None:
                xy, px = self._localize_retry(prompt)
                if px is None:
                    self._position_failed(prompt)
            (cu, cv), _half, accept = self._sweet_box()
            if xy is not None and _inside_box(px, cu, cv, accept):
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
        self._position_failed(prompt)

    # GRASP: search pose -> seed -> servo down -> blind push -> close/twist/lift
    # (wrist_steps=0 skips to blind hover + push)
    def _wrist_seed(self, prompt):
        """Wrist Gemini box -> (center_px, box) or (None, None)."""
        time.sleep(self._p["wrist_settle_s"])
        img = self.wrist_image
        text = (
            gemlib.ask_image(
                self._proxy,
                img,
                f"Wrist camera on a robot gripper, looking down at the floor. "
                f"Find '{prompt}' on the floor. Ignore the gripper fingers "
                "themselves. Return ONLY a JSON list of matches, each "
                '{"box_2d":[ymin,xmin,ymax,xmax]} normalized 0-1000, best first, '
                "each box TIGHT around its object. Empty list if not visible.",
                logger=self.logger,
                cancelled=lambda: self.cancelled,
            )
            if img
            else None
        )
        box = vision.parse_det_box(text)
        px = (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0) if box else None
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
        """Log the exit reason; the (x, y, z) grasp target for every
        _wrist_descend return."""
        self.logger.info(f"[PickAnyObject] wrist stage: {reason} (z={z:.3f})")
        return x, y, z

    def _wrist_reseed(self, prompt, raw):
        """Persistent tracking loss: one Gemini look + a fresh color model,
        since the view has changed. -> (tracker|None, raw, fail_reason)."""
        px, box = self._wrist_seed(prompt)
        if px is None:
            return None, raw, "lost track"
        hsv, raw = self._next_wrist_hsv(raw)
        if hsv is None:
            return None, raw, "no wrist frames"
        tracker = _BlobTracker(hsv, box, px)
        if not tracker.ok:
            return None, raw, "lost track"
        return tracker, raw, ""

    def _wrist_descend(self, prompt, tx, ty):
        """Wrist CamShift servo down to wrist_stop_z: nudge toward the wrist
        box, or step down once the object has been seen inside it twice.
        A miss gets 2 frames of patience, then a Gemini re-seed (budget =
        wrist_steps - 1). Color model, not LK: the object grows/deforms
        during the descent and optical flow slides off.
        Returns (x,y,z); falls back to (tx,ty) if never seen."""
        p = self._p
        ee = self.manipulation.ee_xyz()
        z = ee[2] if ee else p["hover_z"]
        looks = int(p["wrist_steps"]) - 1

        px, box = self._wrist_seed(prompt)
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
        stalled = 0  # consecutive steps eaten by the reach clamp
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
                tracker, raw, fail = self._wrist_reseed(prompt, raw)
                if tracker is None:
                    reason = fail
                    break
                px = tracker.guess  # the re-seed detection is this frame's fix
            streak += 1

            err_u = px[0] - p["wrist_box_u"]
            err_v = px[1] - p["wrist_box_v"]
            inside = _inside_box(px, p["wrist_box_u"], p["wrist_box_v"], p["wrist_half_px"])
            centered = centered + 1 if inside else 0
            if streak < 2:
                continue  # watch one more frame before trusting it
            if inside and centered < 2:
                continue  # just entered the box — confirm it stays

            stepped_down = centered >= 2
            if stepped_down:
                z = max(p["wrist_stop_z"], z - p["wrist_z_step"])
                # descent barely moves a centered pixel: guess stays px
            else:
                # Gains tuned at z=0.15; scale with camera height.
                s = (z + WRIST_CAM_ABOVE_EE) / (0.15 + WRIST_CAM_ABOVE_EE)
                cap = p["wrist_step_max"]
                step_x = max(-cap, min(cap, p["wrist_kx"] / 100.0 * err_v * s))
                step_y = max(-cap, min(cap, p["wrist_ky"] / 100.0 * err_u * s))
                nx, ny = self.manipulation.clamp_reach(x + step_x, y + step_y)
                # The reach clamp can eat the whole step (object parked at the
                # edge of the reach box, so the pixel error can never close).
                # Without this exit the servo re-commands the same clamped pose
                # until the timeout — dipping the arm every move and eventually
                # tripping move_checked recovery. Hand off to the blind push.
                if math.hypot(nx - x, ny - y) < 0.25 * math.hypot(step_x, step_y):
                    stalled += 1
                    if stalled >= 3:
                        reason = "reach limit"
                        break
                    continue  # same clamped pose — don't re-command it
                stalled = 0
                x, y = nx, ny
                # feed-forward: expect the blob at the box center next frame
                tracker.guess = (p["wrist_box_u"], p["wrist_box_v"])
            self.manipulation.move_checked(
                x, y, z, pitch=p["wrist_pitch"], duration=p["wrist_move_s"], logger=self.logger
            )
            if stepped_down:
                # A pure z-hop barely shifts the view: keep the centered
                # verdict and ask for just one fresh confirming frame, so
                # hops chain instead of re-earning 2+2 frames each time.
                streak = 1
            else:
                streak = 0
                centered = 0  # view shifted — re-confirm centering

        return self._wrist_done(x, y, z, reason)

    def _goto_search_pose(self, bearing):
        """WRIST_SEARCH_ARM aimed at bearing; pins IK to elbow-up branch.

        j6 commands GRIPPER_OPEN (what gripper_open itself commands at 100%)
        instead of echoing a live j6 read: right after the chained gripper_open
        child the parent's joint_states snapshot is one 50 Hz tick stale, and
        re-commanding it would close the just-opened gripper."""
        a = WRIST_SEARCH_ARM
        pose = [bearing, a[1], a[2], self._p["wrist_pitch"] - a[1] - a[2], a[4], self.manipulation.GRIPPER_OPEN]
        self.manipulation.go(pose, duration=self._p["hover_s"], is_cancelled=lambda: self.cancelled, logger=self.logger)
        time.sleep(0.3)

    def _open_gripper_checked(self):
        """Open gripper (the skill handles trip recovery). False if still shut."""
        try:
            self.gripper_open()
        except SkillFailed:
            return False
        return True

    def _push_to_floor(self, x, y, z_from):
        """Blind descent to floor as ONE multi-waypoint trajectory — the
        rung-by-rung version decelerated to a stop at every rung and looked
        choppy. Contact just stalls the final segments; abort if still high.
        No mid-descent cancel any more: the whole glide is a few seconds and
        the fingers commit right after anyway."""
        p = self._p
        self._checkpoint()
        rungs = [z for z in (p["descend_z1"], p["descend_z2"], p["descend_z3"], p["floor_z"]) if z < z_from - 1e-6]
        if rungs:
            if len(rungs) >= 2:  # the trajectory service needs >= 2 waypoints
                poses = [{"x": x, "y": y, "z": z, "roll": 0.0, "pitch": p["arm_pitch"], "yaw": 0.0} for z in rungs]
                ok = self.manipulation.move_cartesian_trajectory(
                    poses,
                    segment_duration=p["descend_s"],
                    gripper_position=self.manipulation.GRIPPER_OPEN,  # held open; skip the stale-actual read
                )
            else:
                ok = self.manipulation.move_to_cartesian_pose(
                    x=x,
                    y=y,
                    z=rungs[0],
                    roll=0.0,
                    pitch=p["arm_pitch"],
                    yaw=0.0,
                    duration=p["descend_s"],
                    blocking=True,
                )
            if not ok:
                self.manipulation.recover(self.logger)
        # This guard effectively covers the blind path only: after a wrist
        # align the EE already starts near wrist_stop_z, well below the abort
        # height — a limp arm there is caught by _grasp_verified instead.
        ee = self.manipulation.ee_xyz()
        if ee is not None and ee[2] > p["descend_abort_z"]:
            self.manipulation.recover(self.logger)
            raise ArmUnhealthy("arm would not descend")

    def _arm_joints(self):
        """The 6 current joint positions; raises LookupError when joint
        states are missing or short (callers fall back to IK)."""
        js = self.joint_states
        if js is None or len(js.position) < 6:
            raise LookupError("joint states missing or short")
        return list(js.position[:6])

    def _close_twist_lift(self, x, y):
        """Close, joint-space twist+lift (IK would unwind j5). Grip force is
        the gripper servo's hardware current cap (current-based position
        control, arm_config.yaml): the deep close command stalls on the
        object and squeezes continuously at that constant, safe force, so no
        software stall detection or force loops are needed. Closing on air
        reaches ~GRIPPER_EMPTY_J6 instead, which _grasp_verified's j6 check
        catches."""
        p = self._p
        # Un-press first so the fingers close around the object, not into it.
        if p["close_lift_m"] > 0:
            ee = self.manipulation.ee_xyz()
            if ee is not None:
                self.manipulation.move_to_cartesian_pose(
                    x=x,
                    y=y,
                    z=ee[2] + p["close_lift_m"],
                    roll=0.0,
                    pitch=p["arm_pitch"],
                    yaw=0.0,
                    duration=0.5,
                    blocking=True,
                )
        self.gripper_close(strength=p["close_strength"], duration=p["close_s"])
        # Fingers have committed: from here teardown must fold with the grip
        # kept, not open over the floor mid-carry — only a verified miss
        # clears the flag. Set here, not after _grasp_at returns: an exception
        # in the twist/lift below (e.g. ArmUnhealthy from the LookupError
        # fallback) must not release a just-grasped object on the way home.
        self._holding = True
        time.sleep(p["close_settle_s"])

        grip = -p["close_strength"]
        # The twist exists to wind FABRIC onto the fingers; on a rigid shell
        # it just rotates the grip against the floor and helps eject the
        # object. Gemini's grip_strength doubles as the hardness signal.
        soft = self._grip_strength is None or self._grip_strength >= SOFT_GRIP_MIN
        try:
            if soft:
                j = self._arm_joints()
                j[4] = max(-1.4, min(1.4, j[4] + p["twist_rad"]))
                j[5] = grip
                self.manipulation.move_to_joint_positions(joint_positions=j, duration=1.0, blocking=True)
                time.sleep(0.3)
            j = self._arm_joints()
            j[1] = max(-1.4, j[1] - p["lift_rad"])
            j[5] = grip
            self.manipulation.move_to_joint_positions(joint_positions=j, duration=2.0, blocking=True)
            time.sleep(0.3)
        except LookupError:  # joint states missing/short
            # gripper=grip, never the default: the default re-seeds j6 from the
            # measured (stalled) position, zeroing the mode-5 grip preload —
            # the object would drop out mid-lift.
            self.manipulation.move_checked(
                x, y, 0.22, pitch=p["arm_pitch"], duration=2.0, tol_xy=0.10, gripper=grip, logger=self.logger
            )

    def _grasp_at(self, prompt, xy):
        """Full grasp at floor xy (base_link). Reads self._p params."""
        p = self._p
        x, y = self.manipulation.clamp_reach(xy[0] - p["grasp_x_off"], xy[1])

        if not self._open_gripper_checked():
            raise ArmUnhealthy("gripper would not open")
        if p["wrist_steps"] >= 1:
            self._goto_search_pose(math.atan2(y, x))
            x, y, z = self._wrist_descend(prompt, x, y)
        else:
            z = p["hover_z"]
            self.manipulation.move_checked(x, y, z, pitch=p["arm_pitch"], duration=p["hover_s"], logger=self.logger)

        self._push_to_floor(x, y, z)
        self._checkpoint()  # last exit before the fingers commit
        self._close_twist_lift(x, y)

    def _grasp_verified(self, prompt):
        """Back up, then check floor clear + gripper not open. Gemini gets both
        cameras: the wrist view can show the object in the fingers, so a held
        object isn't mistaken for a dropped one — including one still gripped
        but touching the floor, which counts as grabbed."""
        self._drive(-VERIFY_BACKUP_M)
        time.sleep(self._p["settle_s"])
        j6 = self.manipulation.gripper_j6(self.joint_states)
        main_img, wrist_img = self.main_image, self.wrist_image
        images = [img for img in (main_img, wrist_img) if img]
        # Label each image by the camera that actually supplied it: with the
        # head feed down, image 1 IS the wrist view, and calling it a
        # floor-facing head camera would misread a held object as dropped.
        labels = []
        if main_img:
            labels.append(f"Image {len(labels) + 1} is the head camera looking at the floor.")
        if wrist_img:
            labels.append(
                f"Image {len(labels) + 1} is the WRIST camera next to the gripper "
                "fingers (mirrored) — the object may be visible held in the fingers there."
            )
        floor_text = (
            gemlib.ask_image(
                self._proxy,
                images,
                f"Robot just tried to pick up '{prompt}'. {' '.join(labels)} "
                f"Is '{prompt}' lying loose on the floor/carpet, OUT of the "
                "robot's gripper? An object held between the gripper fingers "
                "counts as grabbed even if it is still touching or resting on "
                "the floor — answer NO for that, as for anything hanging from "
                "the gripper. Answer YES only if the object is on the floor "
                "free of the gripper. Answer only YES or NO.",
                logger=self.logger,
                cancelled=lambda: self.cancelled,
            )
            if images
            else None
        )
        j6_ok = j6 is not None and j6 > GRIPPER_EMPTY_J6 + 0.02
        # Token scan, not a prefix match: replies like "The object is not on
        # the floor." answer correctly without leading with the word, and the
        # old anchored match counted them — and an empty reply — as
        # floor-not-clear, reporting a demonstrably held object as a miss and
        # then releasing it in teardown. \b keeps hedges out: "CANNOT" and
        # "NOT SURE" contain no whole-word NO or YES.
        verdict = (floor_text or "").upper()
        said_no = re.search(r"\bNO\b", verdict) is not None
        said_yes = re.search(r"\bYES\b", verdict) is not None
        if said_no == said_yes:
            # Empty, hedged, or contradictory reply — same as no vision
            # verdict at all (no cameras, or the call failed after retries):
            # fall back to the gripper evidence alone rather than report a
            # demonstrably held object as a missed grasp.
            held = j6_ok
        else:
            held = said_no and j6_ok
        self.logger.info(
            f"[PickAnyObject] verify: floor={floor_text!r} j6={j6} "
            f"({len(images)} cams) -> {'HELD' if held else 'NOT HELD'}"
        )
        return held

    def execute(self, prompt: str = "the sock") -> SkillReturn:
        """Pick up `prompt` from the floor."""
        if self._proxy is None:
            return "Innate proxy not configured (INNATE_SERVICE_KEY)", SkillResult.FAILURE

        # Stop the base the instant a cancel lands (the base cancel() also
        # forwards the cancel to whichever chained child skill is running).
        self.on_cancel(self._stop_base)

        self._grip_strength = None  # singleton: don't carry the last run's object
        self._holding = False
        try:
            self.head.set_position(int(round(self._p["tilt_deg"])))
            # Rest arm first so it doesn't occlude the head camera.
            self.arm_rest()

            self.say(f"Looking for {prompt}.")
            xy = self._search(prompt)
            xy = self._position_above(prompt, xy)
            self.say("Picking it up.")
            self._grasp_at(prompt, xy)
            # _close_twist_lift latched self._holding the moment the fingers
            # committed — only a verified miss clears it.
            if not self._grasp_verified(prompt):
                self._holding = False
                self.say("I couldn't get a grip on it.")
                raise SkillFailed(f"Grasp missed — '{prompt}' is still on the floor (verified after backing up)")
            self.say("Got it.")
            return (
                f"Picked up '{prompt}' (verified: floor clear after backing up)",
                SkillResult.SUCCESS,
            )
        except (SkillCancelled, ArmCancelled):
            return "Pick cancelled", SkillResult.CANCELLED
        except (SkillFailed, ArmFailed) as e:
            return str(e), SkillResult.FAILURE
        except ArmUnhealthy as e:
            self.say("My arm isn't responding properly, stopping.")
            return f"Arm servo failure: {e}", SkillResult.FAILURE
        finally:
            self._stop_base()
            self._rest_arm(keep_grip=self._holding)
            self.head.set_position(0)  # non-None: required interface, server-enforced

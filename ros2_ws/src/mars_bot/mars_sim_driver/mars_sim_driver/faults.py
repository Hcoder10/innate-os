# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Fault injection for the virtual MARS driver: reproduce the calibration
and sensing errors suspected of breaking depth-based obstacle avoidance on
the real robot, so Nav2/STVL can be tested against them in sim.

Configured by the VIRTUAL_MARS_FAULTS env var -- a JSON object; unset (or
{}) means no faults. Every magnitude is a standard deviation and the actual
error is DRAWN ONCE at startup (calibration errors are biases, not per-frame
noise); node.py logs the drawn values so an experiment knows its injected
ground truth. Keys:

  seed                  int   RNG seed for reproducible draws (default random)
  head_angle_bias_deg   float std of a fixed bias on the reported joint_head
                              (encoder zero drift: TF pitches, render doesn't)
  head_angle_jitter_deg float std of extra per-reading joint_head noise
  camera_height_std_m   float std of the actual camera's height offset --
                              moves the rendered viewpoint, TF stays nominal
  camera_rot_std_deg    float std per axis of a camera mount rotation error
                              (extrinsic miscalibration), camera-local axes
  calib_error_frac      float relative std of stereo-calibration error: scales
                              the published fx/fy, shifts cx/cy, and scales
                              depth (baseline error)
  depth_artifacts       bool  stereo depth artifacts: disparity-domain noise
                              (depth error grows with z^2), subpixel
                              quantization, dropout at depth discontinuities,
                              speckle holes
  disparity_noise_px    float disparity noise std, full-res pixels (default 0.5)
  speckle_frac          float fraction of the image dropped as speckle holes
                              (default 0.02)

Example:
  VIRTUAL_MARS_FAULTS='{"seed": 7, "head_angle_bias_deg": 3.0,
  "camera_height_std_m": 0.01, "calib_error_frac": 0.05,
  "depth_artifacts": true}'

The camera mount offset is applied to the world server once at driver
startup; if the world server restarts mid-run, restart the driver too.
"""

import json
import math
import os
import threading

import numpy as np

STEREO_BASELINE_M = 0.06  # mars.urdf: head_camera_left y=+0.0297, right y=-0.0303
DISPARITY_QUANT_PX = 1.0 / 16.0  # SGM subpixel resolution
EDGE_JUMP_PX = 4.0  # disparity step between neighbors that stereo can't match across
SPECKLE_BLOCK = 4  # speckle holes are blobs of failed matching, not lone pixels

_KNOWN_KEYS = {
    "seed",
    "head_angle_bias_deg",
    "head_angle_jitter_deg",
    "camera_height_std_m",
    "camera_rot_std_deg",
    "calib_error_frac",
    "depth_artifacts",
    "disparity_noise_px",
    "speckle_frac",
}


class FaultInjector:
    def __init__(self, focal_px: float, cx: float, cy: float):
        raw = os.environ.get("VIRTUAL_MARS_FAULTS", "").strip()
        cfg = json.loads(raw) if raw else {}
        unknown = set(cfg) - _KNOWN_KEYS
        if unknown:
            # Fail loud: a typoed key would silently run the experiment faultless.
            raise ValueError(f"VIRTUAL_MARS_FAULTS: unknown keys {sorted(unknown)}, known: {sorted(_KNOWN_KEYS)}")
        seed = cfg.get("seed")
        rng = np.random.default_rng(seed)
        # Head readings are perturbed from executor threads while depth is
        # corrupted on the render thread; Generators aren't thread-safe.
        self._head_rng = np.random.default_rng(None if seed is None else seed + 1)
        self._head_lock = threading.Lock()
        self._depth_rng = np.random.default_rng(None if seed is None else seed + 2)

        self.head_bias_rad = math.radians(rng.normal(0.0, float(cfg.get("head_angle_bias_deg", 0.0))))
        self.head_jitter_rad = math.radians(float(cfg.get("head_angle_jitter_deg", 0.0)))
        dz = rng.normal(0.0, float(cfg.get("camera_height_std_m", 0.0)))
        # The camera's parent body is the URDF optical frame (x right, y down,
        # z forward): a world height offset is -y there. Exact at level head,
        # off by cos(head pitch) when tilted -- negligible for cm-scale errors.
        self.cam_dpos = (0.0, -dz, 0.0)
        self.cam_drot_deg = tuple(rng.normal(0.0, float(cfg.get("camera_rot_std_deg", 0.0)), size=3))

        frac = float(cfg.get("calib_error_frac", 0.0))
        self.focal = focal_px * (1.0 + rng.normal(0.0, frac))
        self.cx = cx * (1.0 + rng.normal(0.0, frac))
        self.cy = cy * (1.0 + rng.normal(0.0, frac))
        self.depth_scale = 1.0 + rng.normal(0.0, frac)  # stereo baseline error
        self.artifacts = bool(cfg.get("depth_artifacts", False))
        self.disparity_noise_px = float(cfg.get("disparity_noise_px", 0.5))
        self.speckle_frac = float(cfg.get("speckle_frac", 0.02))
        self._fb = focal_px * STEREO_BASELINE_M  # true optics: disparity = _fb / z
        self._nominal = (focal_px, cx, cy)

    @property
    def wants_camera_perturb(self) -> bool:
        return any(self.cam_dpos) or any(self.cam_drot_deg)

    def head_reading(self, rad: float) -> float:
        """The encoder's version of the true head angle."""
        if self.head_jitter_rad:
            with self._head_lock:
                rad += self._head_rng.normal(0.0, self.head_jitter_rad)
        return rad + self.head_bias_rad

    def corrupt_depth(self, depth: np.ndarray) -> np.ndarray:
        """Meters in, meters out; dropped pixels become 0 (invalid)."""
        if not self.artifacts:
            return depth if self.depth_scale == 1.0 else depth * self.depth_scale
        z = np.asarray(depth, dtype=np.float32)
        with np.errstate(divide="ignore"):
            d = self._fb / z
        d = d + self._depth_rng.normal(0.0, self.disparity_noise_px, size=d.shape)
        d = np.round(d / DISPARITY_QUANT_PX) * DISPARITY_QUANT_PX
        dead = d <= 0
        # Stereo can't match across depth discontinuities: kill both sides of
        # any neighbor pair whose disparity jumps too far, like the real
        # pipeline's edge/speckle filters.
        jump_x = np.abs(np.diff(d, axis=1)) > EDGE_JUMP_PX
        jump_y = np.abs(np.diff(d, axis=0)) > EDGE_JUMP_PX
        dead[:, :-1] |= jump_x
        dead[:, 1:] |= jump_x
        dead[:-1, :] |= jump_y
        dead[1:, :] |= jump_y
        if self.speckle_frac > 0:
            h, w = z.shape
            blocks_h, blocks_w = -(-h // SPECKLE_BLOCK), -(-w // SPECKLE_BLOCK)
            holes = self._depth_rng.random((blocks_h, blocks_w)) < self.speckle_frac
            dead |= np.repeat(np.repeat(holes, SPECKLE_BLOCK, 0), SPECKLE_BLOCK, 1)[:h, :w]
        with np.errstate(divide="ignore"):
            z = (self._fb / d) * self.depth_scale
        z[dead] = 0.0
        return z.astype(np.float32)

    def describe(self) -> list[str]:
        """Drawn values, one line per active fault -- log these."""
        focal0, cx0, cy0 = self._nominal
        lines = []
        if self.head_bias_rad:
            lines.append(f"joint_head reading bias {math.degrees(self.head_bias_rad):+.2f} deg")
        if self.head_jitter_rad:
            lines.append(f"joint_head reading jitter std {math.degrees(self.head_jitter_rad):.2f} deg")
        if self.wants_camera_perturb:
            dpos = ", ".join(f"{v:+.4f}" for v in self.cam_dpos)
            drot = ", ".join(f"{v:+.2f}" for v in self.cam_drot_deg)
            lines.append(f"camera mount offset ({dpos}) m, rotation ({drot}) deg (TF stays nominal)")
        if (self.focal, self.cx, self.cy) != (focal0, cx0, cy0) or self.depth_scale != 1.0:
            lines.append(
                f"calibration: f {self.focal:.1f} (true {focal0:.1f}), "
                f"c ({self.cx:.1f}, {self.cy:.1f}) (true ({cx0:.1f}, {cy0:.1f})), "
                f"depth scale x{self.depth_scale:.4f}"
            )
        if self.artifacts:
            lines.append(
                f"depth artifacts: disparity noise {self.disparity_noise_px} px, "
                f"quantization 1/{round(1 / DISPARITY_QUANT_PX)} px, edge dropout, "
                f"speckle holes {self.speckle_frac:.1%}"
            )
        return lines

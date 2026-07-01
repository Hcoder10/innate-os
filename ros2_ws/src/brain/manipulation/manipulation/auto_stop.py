"""Auto-stop logic for learned behaviors.

A learned ACT policy never ends on its own -- it just keeps emitting action
chunks -- so ``manipulation_server`` runs it in an open-ended loop bounded only
by a wall-clock ``duration`` cap. :class:`LearnedStopDetector` lets a skill end
*early*, when it's actually done, by watching two signals the policy already
produces each step:

- ``progress`` (action[8]): the trained progress head, 0 -> ~1 over a demo.
- robot motion: the L2 change in the commanded joint targets (``arm_delta``)
  and the commanded base velocity magnitude (``base_speed``). Both fall to ~0
  once the policy converges on a final pose and holds still.

Kept ROS-free (stdlib only) so it can be unit-tested without a ROS environment,
matching :mod:`manipulation.config_validation`.
"""

from __future__ import annotations

import math
from collections import namedtuple

# Per-step signals the learned loop feeds the detector. ``progress`` is None for
# skills whose action head is < 10 wide; ``arm_delta`` is None on the first step
# (no previous action to diff against).
StepSignals = namedtuple("StepSignals", ["progress", "arm_delta", "base_speed"])


class LearnedStopDetector:
    """Decides when a learned behavior should stop on its own.

    Fed one :class:`StepSignals` per inference step via :meth:`update`, it fires
    when either

    - smoothed progress exceeds ``progress_threshold``, or
    - the robot has held still (arm *and* base below their idle thresholds) for
      ``idle_seconds`` while smoothed progress is at least ``progress_gate`` -- the
      "stopped moving, and it's made progress" rule,

    once ``elapsed`` clears the ``min_duration`` floor. The caller keeps
    ``duration`` as the always-on hard cap. A fresh detector is built per behavior,
    so it carries no state across runs.

    Defaults are no-ops (idle off, no smoothing, no floor), so a skill that sets
    none of the knobs reduces to the legacy single-frame ``progress >
    progress_threshold`` check.
    """

    def __init__(
        self,
        *,
        min_duration: float = 0.0,
        progress_threshold: float = 2.0,
        progress_ema_alpha: float = 1.0,
        idle_seconds: float = 0.0,
        idle_arm_eps: float = 0.01,
        idle_base_eps: float = 0.01,
        progress_gate: float = 0.0,
    ):
        self.min_duration = min_duration
        self.progress_threshold = progress_threshold
        self.progress_ema_alpha = progress_ema_alpha
        self.idle_seconds = idle_seconds
        self.idle_arm_eps = idle_arm_eps
        self.idle_base_eps = idle_base_eps
        self.progress_gate = progress_gate
        self._progress_ema: float | None = None
        self._idle_since: float | None = None

    def _smooth(self, progress: float | None) -> float | None:
        """EMA-smooth progress; carry the last value forward when progress is missing.

        Non-finite progress (a diverging policy emits NaN) is treated as missing:
        blending it in would poison the EMA for the rest of the run (NaN survives the
        convex combination), permanently disabling the progress-threshold stop.
        """
        if progress is None or not math.isfinite(progress):
            return self._progress_ema
        if self._progress_ema is None:
            self._progress_ema = progress
        else:
            a = self.progress_ema_alpha
            self._progress_ema = a * progress + (1.0 - a) * self._progress_ema
        return self._progress_ema

    def update(self, signals: StepSignals, elapsed: float, now: float):
        """Return ``(stop, reason)`` for this step (``reason`` is None when not stopping).

        ``elapsed`` is seconds since the behavior started; ``now`` is a monotonic-ish
        wall-clock timestamp used only for the idle dwell timer (the caller passes the
        loop's ``time.time()``).
        """
        progress = self._smooth(signals.progress)

        # Inside the min_duration floor nothing may fire, and the idle dwell doesn't
        # accumulate either: demos often begin with a still beat before the arm moves,
        # and a timer running through the floor would fire "idle" the instant the floor
        # expires. Resetting here means the robot must hold still for a full
        # idle_seconds *after* the floor. (The EMA above still warms up during it.)
        if elapsed < self.min_duration:
            self._idle_since = None
            return False, None

        # Track how long the robot has been holding still. Missing motion signals
        # (first step) and non-finite ones (a diverging policy emits NaN, which would
        # compare False against the eps and read as "still") count as "moving" so we
        # never stop on them.
        moving = (
            signals.arm_delta is None
            or signals.base_speed is None
            or not math.isfinite(signals.arm_delta)
            or not math.isfinite(signals.base_speed)
            or signals.arm_delta >= self.idle_arm_eps
            or signals.base_speed >= self.idle_base_eps
        )
        if moving:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = now

        if progress is not None and progress > self.progress_threshold:
            return True, f"progress {progress:.4f} > {self.progress_threshold}"

        if self.idle_seconds > 0 and self._idle_since is not None:
            held = now - self._idle_since
            gate_ok = self.progress_gate <= 0 or (progress is not None and progress >= self.progress_gate)
            if held >= self.idle_seconds and gate_ok:
                note = "" if self.progress_gate <= 0 else f", progress {progress:.4f} >= {self.progress_gate}"
                return True, f"idle for {held:.1f}s (>= {self.idle_seconds}s){note}"

        return False, None

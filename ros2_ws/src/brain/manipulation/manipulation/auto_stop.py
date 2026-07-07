"""Auto-stop for learned behaviors. An ACT policy never ends on its own -- it just
keeps emitting action chunks -- so the executor loop is bounded only by a wall-clock
``duration`` cap. :class:`LearnedStopDetector` lets a skill end early, when it's
actually done, from the trained progress head (action[8], 0 -> ~1 over a demo).

ROS-free (stdlib only) so it can live in its own module without a ROS environment,
like :mod:`manipulation.config_validation`.
"""

from __future__ import annotations

import math
from collections import namedtuple

# One inference step's signals. progress is None when the action head is < 10 wide.
StepSignals = namedtuple("StepSignals", ["progress"])


class LearnedStopDetector:
    """Decides when a learned behavior should stop on its own.

    Fed one :class:`StepSignals` per step via :meth:`update`, it fires when either:

    - EMA-smoothed progress exceeds ``progress_threshold`` (single-frame legacy check);
    - smoothed progress, having first dipped below ``engage_below``, then holds at or
      above ``stable_min`` for ``stable_seconds`` (progress-stability stop -- for
      checkpoints whose progress head saturates high both before and after the task).

    Both only after the ``min_duration`` floor; the caller keeps ``duration`` as the
    always-on hard cap.

    Defaults are no-ops (stability off, no smoothing, no floor): an unconfigured
    detector reduces to the legacy single-frame ``progress > progress_threshold``
    check. Build a fresh detector per behavior run.
    """

    def __init__(
        self,
        *,
        min_duration: float = 0.0,
        progress_threshold: float = 2.0,
        progress_ema_alpha: float = 1.0,
        engage_below: float = 0.0,
        stable_min: float = 0.0,
        stable_seconds: float = 0.0,
    ):
        self.min_duration = min_duration
        self.progress_threshold = progress_threshold
        self.progress_ema_alpha = progress_ema_alpha
        self.engage_below = engage_below
        self.stable_min = stable_min
        self.stable_seconds = stable_seconds
        self._progress_ema: float | None = None
        self._engaged = False
        self._stable_since: float | None = None

    def note_gap(self, seconds: float):
        """Discount a step the detector never saw (inference failed) from the stability
        dwell: the dwell must only count observed samples, but a lone dropped step must
        not throw away seconds of genuinely observed stability -- so push the dwell start
        forward by the unobserved window instead of restarting it. Engagement is a
        latched fact about the run, so a gap doesn't clear it."""
        if self._stable_since is not None:
            self._stable_since += seconds

    def update(self, signals: StepSignals, elapsed: float, now: float):
        """One step -> ``(stop, reason)``. ``elapsed`` is seconds since the behavior
        started; ``now`` is the loop's ``time.time()``, used for the stability dwell."""
        # EMA-smooth progress. Missing and non-finite samples are skipped, carrying
        # the previous value forward: blending NaN in would poison the EMA for the
        # rest of the run (NaN survives the convex combination).
        p = signals.progress
        if p is not None and math.isfinite(p):
            a = self.progress_ema_alpha
            self._progress_ema = p if self._progress_ema is None else a * p + (1.0 - a) * self._progress_ema
        ema = self._progress_ema

        # Inside the floor nothing fires and the dwell doesn't accumulate: demos often
        # start with a still, high-progress beat, and a dwell running through the floor
        # would fire the instant it expires. (The EMA above still warms up.)
        if elapsed < self.min_duration:
            self._stable_since = None
            return False, None

        if ema is not None and ema > self.progress_threshold:
            return True, f"progress {ema:.4f} > {self.progress_threshold}"

        # Progress-stability stop. The progress head can saturate near its max both
        # before the task engages and after it finishes, so "progress is high" alone
        # fires in the opening steps. Requiring progress to first dip below
        # ``engage_below`` (the policy actually did something) arms the stop; it then
        # fires once smoothed progress holds >= ``stable_min`` for ``stable_seconds``.
        # ``engage_below <= 0`` arms immediately (bare stability, no dip required).
        if self.stable_seconds > 0 and self.stable_min > 0 and ema is not None:
            if self.engage_below <= 0 or ema < self.engage_below:
                self._engaged = True
            if self._engaged and ema >= self.stable_min:
                if self._stable_since is None:
                    self._stable_since = now
                held = now - self._stable_since
                if held >= self.stable_seconds:
                    return True, f"progress stable >= {self.stable_min} for {held:.1f}s (>= {self.stable_seconds}s)"
            else:
                self._stable_since = None

        return False, None

# manipulation

Arm control and skill execution: `manipulation_server` runs learned (ACT),
pose, and replay behaviors as the `execute_skill` action server. This README
covers the part with the most non-obvious tuning surface — **auto-stop for
learned skills**. For the schema itself see
[`manipulation/config_validation.py`](manipulation/config_validation.py); for
the detector, [`manipulation/auto_stop.py`](manipulation/auto_stop.py).

## Auto-stop for learned skills

An ACT policy never terminates on its own — it keeps emitting action chunks
until the wall-clock `duration` cap (default 120 s). `LearnedStopDetector`
lets a skill end early, when it's actually done, using the policy's trained
progress head (`action[8]`, roughly 0 → 1 over a demo).

It is **opt-in per skill**, in the skill's `metadata.json` (under
`workspace/custom_skills/<skill>/`):

```json
"execution": {
  "auto_stop": true
}
```

`auto_stop: false` (the default) ignores every knob below and runs to the
`duration` cap, exactly as before. Flipping it on fills any knob you left
unset with the recommended engage-then-settle config
(`_AUTO_STOP_DEFAULTS` in `config_validation.py`); knobs you set explicitly
win.

### Knobs

| knob | filled default | meaning |
|---|---|---|
| `duration` | 120 | hard wall-clock cap, always on — auto-stop only ever ends a run *earlier* |
| `min_duration` | 5.0 | floor (s) before any early stop may fire; the stability dwell does **not** accumulate inside it |
| `progress_ema_alpha` | 0.3 | EMA smoothing of the progress head, in (0, 1]; 1.0 = raw |
| `progress_threshold` | 2.0 (≙ off) | legacy single-frame stop: smoothed progress > threshold |
| `engage_below` | 0.75 | arms the stability stop once smoothed progress first dips below it; 0 = armed immediately |
| `stable_min` | 0.93 | …then stop once smoothed progress holds ≥ this… |
| `stable_seconds` | 3.0 | …for this long |

How they combine (`LearnedStopDetector.update`): nothing fires before
`min_duration`; after it, whichever of the progress-threshold or the
engage-then-settle stop fires first ends the run; `duration` remains the
backstop either way. `stable_seconds > 0` requires a progress head
(`action_dim >= 10`, the default) — config validation rejects the combination
otherwise, since the stop would silently never fire.

**`engage_below` is a one-way latch.** Once smoothed progress dips below it —
even from a noise dip that survives the EMA — the run stays "engaged" until
the end. Pick it below anything the pre-engagement idle phase can plausibly
produce, not just below its mean.

### Why engage-then-settle

On real checkpoints the progress head saturates near its max at *both* ends of
a rollout: the opening seconds (arm not yet engaged) read high, stable, and
low-jerk — indistinguishable from the converged tail — with the actual work
(progress dipping to ~0.4–0.7) in the middle. A bare threshold or bare
stability rule therefore fires in the opening steps. Requiring the dip below
`engage_below` first distinguishes "hasn't started" from "finished".

### Tuning workflow (per skill / checkpoint)

1. Profiling page → pick the skill → **Run rollout** (or *Profile only*) and
   let a successful run go to completion.
2. Read the **Progress** chart over the whole run — it plots the full run on a
   time axis, and hovering reads exact (time, value) pairs. The `progress`
   stat card shows the max.
3. `stable_min`: just under the settled tail's plateau.
4. `engage_below`: above the active-phase dips, below the idle plateau.
5. `stable_seconds`: longer than any transient mid-task plateau.
6. Saved rollout episodes carry their full profile trace (Datasets page →
   episode player → *inference profile*), so thresholds can also be read off
   past runs.

The shipped defaults were tuned on **one** skill (phase-0 pick-sock) and are a
starting point, not a validated universal: expect to re-read them off a real
trace per checkpoint.

### Known limitations

- **The progress head is self-reported and can be confidently wrong.** A
  sub-goal state that visually/proprioceptively resembles the final state can
  read as high, stable progress without the task having succeeded.
  `stable_min`/`stable_seconds` filter noise, not systematic error — there is
  currently no independent corroboration of "done".
- **A motion-based idle stop was tried and removed.** Requiring the commanded
  arm/base to be physically still would be exactly that corroboration, but
  ACT's per-chunk replanning keeps the command stream jittering even while the
  robot holds pose (longest still window on phase-0 hardware data: ~0.7 s), so
  the idle rule could never fire at any usable epsilon. If a cleaner stillness
  signal appears (measured joint velocities rather than command deltas), that
  is the natural corroborating check to reintroduce.
- Auto-stop is built for **supervised eval rollouts** (Profiling page, an
  operator watching, a false stop costs one rerun). Unsupervised use — or a
  skill with a poorly calibrated progress head — should treat it as untested
  and lean on the `duration` cap.

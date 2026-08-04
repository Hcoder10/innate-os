# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Electric motor whine for MARS, synthesised from road speed.

MARS is electric, so this is what it actually sounds like -- which is why it
works where a synthesised V8 did not. A combustion engine is a familiar sound
everyone can hear the flaws in, and its defining quality is bass that a 2 W
MAX98357A cannot produce. A motor whine has neither problem: nobody carries a
strong prior for what one robot's motor should sound like, and its energy sits
at 200 Hz to 4 kHz, right where that speaker is strongest.

It is also the rare thing additive synthesis is genuinely good at. A real
motor's whine *is* a harmonic stack -- magnetic pole passing and gear-tooth
meshing produce tones, not the transients an engine needs -- so summing
partials models the mechanism rather than faking it.

The character on top of the bare stack:

* A virtual gearbox. Pitch sweeps up, snaps back at a shift, and sweeps
  again -- the shift gap is the strongest 'it is accelerating' cue there is,
  and it survived from the engine versions because it was the one part of
  them that always worked.
* Brush flutter. A brushed motor's commutator chops the current, which reads
  as amplitude tremolo at a fraction of the whine frequency. That flutter is
  most of the difference between a sterile oscillator and something that
  sounds like it has windings.
* Acceleration growl. Working hard adds a slow irregular throb and a band of
  low-mid strain noise, both scaled by load -- so flooring it rumbles and
  cruising is smooth. Real rumble is below the speaker's floor, so it is
  delivered as amplitude modulation, the same trick as the V8's lope.
* Vibrato: a shallow pitch wobble so a held speed still breathes.
* The motor's speed follows the robot's through a lightly underdamped spring,
  so pulling away overshoots and coming to rest glides down.
* A spin-up chirp on startup -- the whine rising from below into idle, the
  way a machine sounds when it wakes.
* Tire screech on hard turns: a squeal wandering in pitch over slip noise,
  stuttering as the tread grabs and lets go. Gentle steering stays silent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# (frequency ratio, amplitude). The 7.3 is the gear-whine partial: deliberately
# not a whole-number ratio, because a meshing gear is not harmonically related
# to the pole-passing tone, and that slight dissonance is most of what makes a
# whine read as *electric* rather than as an organ.
PARTIALS: tuple[tuple[float, float], ...] = (
    (1.0, 1.00),
    (2.0, 0.55),
    (3.0, 0.26),
    (4.0, 0.15),
    (6.0, 0.07),
    (7.3, 0.13),
)

SCREECH_PARTIALS: tuple[tuple[float, float], ...] = ((1.0, 1.00), (2.0, 0.45), (3.0, 0.20))
"""Squeal is reedy, not a whistle: a fundamental with strong harmonics."""

TREMOLO_RATIO = 12.0
"""The brush flutter sits at whine frequency / this: ~15 Hz at idle rising to
~80 Hz flat out, so it slows to a tremble at rest and hardens to a buzz at
speed, the way commutator ripple actually behaves."""

NOISE_TAPS = 63
"""Length of the rolling-noise bandpass. Long enough to shape the band, short
enough that convolving it per block costs nothing."""


@dataclass(frozen=True)
class MotorConfig:
    """Character of the motor."""

    base_hz: float = 180.0
    """Whine frequency at the bottom of a gear."""
    top_hz: float = 950.0
    """Whine frequency at the top of a gear. The ratio to base_hz is the size
    of each gear's sweep -- the main 'shows speed' control."""
    max_speed: float = 0.4
    """Road speed in m/s at the top of top gear. Matches
    ``motion_control.yaml``'s ``max_speed``."""

    gear_tops: tuple[float, ...] = (0.5, 1.0)
    """Fraction of ``max_speed`` at which each gear tops out. Within a gear
    the whine sweeps base_hz to top_hz, then drops back at the shift."""
    shift_seconds: float = 0.12
    """Cut duration on a gear change. The gap is what reads as a shift."""

    spring_hz: float = 1.2
    """How eagerly the motor's speed chases the robot's. Kept low so the
    heard acceleration is a slow spool-up that deliberately lags the real
    speed rather than tracking it exactly."""
    damping: float = 0.55
    """Below 1 the response overshoots: the swoop on pulling away and the
    glide down when stopping. Around 0.5-0.7 reads as lively; at 1.0 the motor
    tracks speed exactly and sounds mechanical."""

    wobble_hz: float = 5.5
    wobble_depth: float = 0.018
    """Vibrato. Deep enough to be heard as a waver on a held note, shallow
    enough not to sound detuned -- 0.018 is about a third of a semitone."""

    brush_depth: float = 0.35
    """Commutator-flutter tremolo depth, 0..1. Zero is a clean brushless hum;
    higher is brushier, tremblier. The amplitude dips to (1 - depth) at the
    bottom of each flutter cycle."""

    growl_hz: float = 26.0
    growl_depth: float = 0.5
    """Acceleration growl: a slow, irregular throb that comes in with load
    and fades at cruise, so working hard *rumbles*. True rumble is below what
    the speaker can radiate, so it is delivered as amplitude modulation --
    the same trick as the V8's lope -- at growl_hz plus a detuned partner,
    whose beat keeps the throb from sounding like a metronome."""
    growl_noise_level: float = 0.20
    """Low-mid strain noise under load, the motor equivalent of an engine
    roaring on the throttle. Banded above the speaker floor."""

    rolling_level: float = 0.14
    """Broadband rolling and bearing noise, scaled by speed."""
    rolling_band_hz: tuple[float, float] = (300.0, 3000.0)

    screech_level: float = 0.55
    """Tire screech loudness when cornering flat out. Zero removes it."""
    screech_hz: float = 1100.0
    """Squeal fundamental. Real squeal sits around 0.7-1.3 kHz."""
    max_yaw_rate: float = 2.0
    """Commanded yaw in rad/s that counts as cornering flat out. Matches Mad
    mode's turn cap (motion_control's 1.0 rad/s at Mad's 2x scale)."""
    screech_min_turn: float = 0.4
    """Fraction of max_yaw_rate below which turning stays silent, so steering
    corrections and gentle arcs do not squeal."""

    startup_seconds: float = 0.9
    """Length of the spin-up chirp played by ``trigger_startup()``."""

    idle_level: float = 0.12
    """How loud the motor sits when stopped but enabled. Small: a parked robot
    humming continuously gets old fast."""
    volume: float = 0.5


def is_mad_scale(info: dict) -> bool:
    """True when a /robot/info payload says the robot is in Mad mode.

    mars_app publishes the live ``drive_speed_scale`` and the preset table at
    1 Hz; Mad is the preset whose id is "mad", so the robot stays the single
    source of truth for what Mad means -- retuning its scale robot-side never
    desynchronises this. Malformed or pre-speed-mode payloads read as not-mad,
    which fails towards silence.
    """
    scale = info.get("drive_speed_scale")
    modes = info.get("drive_speed_modes")
    if not isinstance(scale, (int, float)) or not isinstance(modes, list):
        return False
    for mode in modes:
        if isinstance(mode, dict) and mode.get("id") == "mad":
            mad_scale = mode.get("scale")
            return isinstance(mad_scale, (int, float)) and abs(scale - mad_scale) < 1e-6
    return False


def _bandpass_kernel(sample_rate: int, low_hz: float, high_hz: float, taps: int = NOISE_TAPS) -> np.ndarray:
    """Windowed-sinc bandpass, normalised so white noise through it comes out
    at unit RMS. Built once; numpy has no IIR filter and a per-sample Python
    loop would be far too slow for the audio thread."""
    index = np.arange(taps) - (taps - 1) / 2

    def lowpass(cutoff_hz: float) -> np.ndarray:
        ratio = 2.0 * cutoff_hz / sample_rate
        return ratio * np.sinc(ratio * index)

    kernel = (lowpass(high_hz) - lowpass(low_hz)) * np.hamming(taps)
    return kernel / max(math.sqrt(float((kernel**2).sum())), 1e-9)


@dataclass
class _Rotor:
    """The motor's own speed, chasing the robot's through a damped spring.

    A first-order filter would just lag. This overshoots slightly on the way up
    and settles back on the way down, which is the whole difference between
    'a robot moving' and something that sounds pleased to be going.
    """

    cfg: MotorConfig
    speed: float = 0.0
    velocity: float = field(default=0.0)

    def update(self, target_speed: float, dt: float) -> float:
        omega = 2.0 * math.pi * self.cfg.spring_hz
        acceleration = omega * omega * (target_speed - self.speed) - 2.0 * self.cfg.damping * omega * self.velocity
        self.velocity += acceleration * dt
        self.speed += self.velocity * dt
        # Never let the overshoot swing negative: the whine has no direction,
        # and a sign flip would fold the pitch back up through zero.
        if self.speed < 0.0:
            self.speed = 0.0
            self.velocity = max(self.velocity, 0.0)
        return self.speed


@dataclass
class _Gearbox:
    """Speed fraction in, position-through-gear out, with shift cuts.

    Ported intact from the engine versions: it was the one part of them that
    always worked. Within a gear the whine sweeps its full range; at a shift
    it snaps back and sweeps again, and the brief cut is the cue."""

    cfg: MotorConfig
    gear: int = 0
    _shift_remaining: float = 0.0

    def update(self, speed_frac: float, dt: float) -> tuple[float, bool]:
        """Advance by ``dt``. Returns ``(through_gear 0..1, shifting)``."""
        tops = self.cfg.gear_tops
        previous = self.gear
        gear = min(self.gear, len(tops) - 1)
        # Hysteresis so a wavering speed does not chatter between two gears.
        while gear < len(tops) - 1 and speed_frac > tops[gear]:
            gear += 1
        while gear > 0 and speed_frac < tops[gear - 1] * 0.88:
            gear -= 1
        self.gear = gear
        if gear != previous:
            self._shift_remaining = self.cfg.shift_seconds

        shifting = self._shift_remaining > 0.0
        if shifting:
            self._shift_remaining = max(0.0, self._shift_remaining - dt)

        low = 0.0 if gear == 0 else tops[gear - 1]
        span = max(tops[gear] - low, 1e-6)
        through = min(max((speed_frac - low) / span, 0.0), 1.0)
        return through, shifting


class MotorSynth:
    """Renders motor audio in blocks. ``set_drive()`` is called from whatever
    thread knows the robot's speed; ``render()`` is called from the audio
    thread. Both only touch plain floats, which is why no lock is needed."""

    def __init__(self, sample_rate: int = 48000, cfg: MotorConfig | None = None, seed: int = 0):
        self.cfg = cfg or MotorConfig()
        self.sample_rate = sample_rate
        self.volume = self.cfg.volume
        self.enabled = True

        self._speed = 0.0
        self._throttle = 0.0
        self._turn = 0.0

        self._rotor = _Rotor(self.cfg)
        self._gearbox = _Gearbox(self.cfg)
        self._rng = np.random.default_rng(seed)
        self._noise_kernel = _bandpass_kernel(sample_rate, *self.cfg.rolling_band_hz)
        self._noise_tail = np.zeros(NOISE_TAPS - 1)
        # Strain noise sits lower than the rolling noise -- that is what makes
        # it read as effort. The long kernel is what holds the low edge: a
        # 63-tap filter at 48 kHz has a ~760 Hz transition band and leaks well
        # below the speaker's ~160 Hz floor, wasting amp headroom there.
        self._growl_kernel = _bandpass_kernel(sample_rate, 240.0, 700.0, taps=255)
        self._growl_tail = np.zeros(len(self._growl_kernel) - 1)
        # Slip noise, banded around the squeal so tone and noise read as one
        # sound rather than two layers.
        self._screech_kernel = _bandpass_kernel(sample_rate, self.cfg.screech_hz * 0.7, self.cfg.screech_hz * 1.7)
        self._screech_tail = np.zeros(NOISE_TAPS - 1)

        # One phase per partial: the gear-whine ratio is not a whole number, so
        # a single shared phase could not be wrapped without a discontinuity.
        self._phases = np.zeros(len(PARTIALS))
        self._wobble_phase = 0.0
        self._tremolo_phase = 0.0
        self._growl_phase_a = 0.0
        self._growl_phase_b = 0.0
        self._load_smooth = 0.0
        """Load eased over ~80 ms so the growl swells in and out instead of
        gating on and off with every /cmd_vel edge."""
        self._screech_phases = np.zeros(len(SCREECH_PARTIALS))
        self._screech_drift = 0.0
        """Pitch offset as a fraction of screech_hz. A random walk, not a
        vibrato: real squeal lurches as grip changes rather than warbling on
        a metronome."""
        self._screech_pitch = 1.0
        self._screech_judder_phase_a = 0.0
        self._screech_judder_phase_b = 0.0
        self._screech_smooth = 0.0
        self._screech_gain = 0.0
        self._frequency = self.cfg.base_hz
        self._startup_elapsed = self.cfg.startup_seconds
        """Time into the spin-up chirp; starts complete so a fresh synth does
        not chirp until asked."""
        self._gain = 0.0
        """Gain applied at the end of the last block. Every change is ramped
        from here, so toggling ``enabled`` or ``volume`` cannot click."""

    def set_drive(self, speed: float, throttle: float, turn: float = 0.0) -> None:
        """Latest road speed (m/s), throttle demand (0..1), and yaw rate
        (rad/s, sign ignored)."""
        self._speed = speed
        self._throttle = throttle
        self._turn = turn

    def trigger_startup(self) -> None:
        """Play the spin-up chirp: the whine rising from below into idle."""
        self._startup_elapsed = 0.0

    def rotor_speed(self) -> float:
        """The motor's own speed, after the spring. Useful for display."""
        return self._rotor.speed

    def render(self, frames: int) -> np.ndarray:
        """Render ``frames`` mono samples as float32 in roughly [-1, 1]."""
        cfg = self.cfg
        dt = frames / self.sample_rate
        speed = self._rotor.update(abs(self._speed), dt)
        load = min(max(self._throttle, 0.0), 1.0)

        speed_frac = min(speed / cfg.max_speed, 1.0) if cfg.max_speed > 0 else 0.0
        through, shifting = self._gearbox.update(speed_frac, dt)
        frequency = cfg.base_hz + through * (cfg.top_hz - cfg.base_hz)

        # Spin-up chirp: sweep in from well below the idle whine, with a small
        # hump past the target near the end -- a wake-up, not a fade-in.
        startup_progress = 1.0
        if self._startup_elapsed < cfg.startup_seconds:
            self._startup_elapsed += dt
            startup_progress = min(self._startup_elapsed / cfg.startup_seconds, 1.0)
            ramp = 0.30 + 0.70 * startup_progress**0.55
            hump = 1.0 + 0.18 * math.exp(-(((startup_progress - 0.70) / 0.16) ** 2))
            frequency *= ramp * hump

        # Working harder both lifts the level and brightens the stack, so
        # accelerating and coasting differ at the same speed.
        level = cfg.idle_level + (1.0 - cfg.idle_level) * min(speed_frac * 1.4, 1.0)
        target_gain = (self.volume if self.enabled else 0.0) * level * (0.75 + 0.25 * load)
        if shifting:
            target_gain *= 0.35
        # The chirp fades in as it rises; the floor keeps the very first block
        # above the silence gate so the startup is never swallowed.
        target_gain *= max(startup_progress, 0.05) ** 0.7

        # Cornering intensity: silent below the threshold, swelling to full
        # squeal at max yaw.
        turn_frac = min(abs(self._turn) / cfg.max_yaw_rate, 1.0) if cfg.max_yaw_rate > 0 else 0.0
        bite = max(turn_frac - cfg.screech_min_turn, 0.0) / max(1.0 - cfg.screech_min_turn, 1e-6)
        # Fast attack, slower release, so it bites turning in and trails off
        # out of the corner instead of chopping with the stick.
        tau = 0.05 if bite > self._screech_smooth else 0.22
        self._screech_smooth += (bite - self._screech_smooth) * min(dt / tau, 1.0)
        if self._screech_smooth < 1e-3:
            # Snap the tail down, or it asymptotes and the gate never closes.
            self._screech_smooth = bite
        screech_target = (self.volume if self.enabled else 0.0) * cfg.screech_level * self._screech_smooth**1.4

        if max(target_gain, screech_target) <= 1e-5 and max(self._gain, self._screech_gain) <= 1e-5:
            # Silent and staying silent: skip synthesis, but keep the frequency
            # tracked so resuming does not jump in pitch.
            self._gain = 0.0
            self._frequency = frequency
            return np.zeros(frames, dtype=np.float32)

        # Sweep frequency across the block rather than stepping it, so hard
        # acceleration does not zipper.
        sweep = np.linspace(self._frequency, frequency, frames)
        self._frequency = frequency

        # Vibrato.
        steps = np.arange(1, frames + 1) / self.sample_rate
        wobble_phase = self._wobble_phase + 2.0 * np.pi * cfg.wobble_hz * steps
        self._wobble_phase = float(wobble_phase[-1] % (2.0 * np.pi))
        sweep = sweep * (1.0 + cfg.wobble_depth * np.sin(wobble_phase))

        nyquist = 0.45 * self.sample_rate
        brightness = 0.35 * load  # more upper partials under load
        signal = np.zeros(frames)
        weight_sum = 0.0
        for index, (ratio, amplitude) in enumerate(PARTIALS):
            partial_hz = sweep * ratio
            phase = self._phases[index] + 2.0 * np.pi * np.cumsum(partial_hz) / self.sample_rate
            self._phases[index] = float(phase[-1] % (2.0 * np.pi))
            weight = amplitude * (1.0 + brightness) if ratio > 1.0 else amplitude
            audible = partial_hz < nyquist
            signal += weight * audible * np.sin(phase)
            weight_sum += weight
        signal /= max(weight_sum, 1e-6)

        # Brush flutter: amplitude tremolo at a fraction of the whine
        # frequency, phase-continuous across blocks like everything else.
        tremolo_phase = self._tremolo_phase + 2.0 * np.pi * np.cumsum(sweep / TREMOLO_RATIO) / self.sample_rate
        self._tremolo_phase = float(tremolo_phase[-1] % (2.0 * np.pi))
        flutter = 1.0 - cfg.brush_depth * 0.5 * (1.0 + np.sin(tremolo_phase))

        # Acceleration growl: an irregular throb that swells with load. Two
        # detuned modulators beat against each other so the throb rolls
        # rather than ticking.
        self._load_smooth += (load - self._load_smooth) * min(dt / 0.08, 1.0)
        strain = cfg.growl_depth * self._load_smooth
        phase_a = self._growl_phase_a + 2.0 * np.pi * cfg.growl_hz * steps
        phase_b = self._growl_phase_b + 2.0 * np.pi * (cfg.growl_hz * 1.61) * steps
        self._growl_phase_a = float(phase_a[-1] % (2.0 * np.pi))
        self._growl_phase_b = float(phase_b[-1] % (2.0 * np.pi))
        throb = 1.0 - strain * 0.5 * (1.0 + 0.62 * np.sin(phase_a) + 0.38 * np.sin(phase_b))

        signal *= flutter * throb

        # The rolling noise flutters with the brushes too, but only partly --
        # the wheels on the floor do not care what the commutator is doing.
        signal += self._rolling_noise(frames, speed_frac) * (0.4 + 0.6 * flutter)
        # Strain noise: the low-mid roar of working hard, throbbing with the
        # growl and gone at cruise.
        signal += self._growl_noise(frames) * self._load_smooth * throb

        gain = np.linspace(self._gain, target_gain, frames)
        self._gain = target_gain
        mix = signal * gain + self._screech(frames, steps, screech_target)
        return (np.tanh(mix * 1.6) * 0.85).astype(np.float32)

    def _screech(self, frames: int, steps: np.ndarray, target: float):
        """Tire squeal: a harmonic stack whose pitch wanders as grip changes,
        over slip noise, stuttering as the tread grabs and lets go."""
        gain = np.linspace(self._screech_gain, target, frames)
        self._screech_gain = target
        if target <= 1e-5 and gain[0] <= 1e-5:
            return 0.0

        dt = frames / self.sample_rate
        self._screech_drift += -self._screech_drift * (dt / 0.15) + 0.11 * math.sqrt(dt) * float(
            self._rng.standard_normal()
        )
        self._screech_drift = min(max(self._screech_drift, -0.08), 0.08)
        pitch = np.linspace(self._screech_pitch, 1.0 + self._screech_drift, frames)
        self._screech_pitch = 1.0 + self._screech_drift
        tone_hz = self.cfg.screech_hz * pitch

        tone = np.zeros(frames)
        weight_sum = 0.0
        for index, (ratio, amplitude) in enumerate(SCREECH_PARTIALS):
            phase = self._screech_phases[index] + 2.0 * np.pi * np.cumsum(tone_hz * ratio) / self.sample_rate
            self._screech_phases[index] = float(phase[-1] % (2.0 * np.pi))
            tone += amplitude * np.sin(phase)
            weight_sum += amplitude
        tone /= weight_sum

        padded = np.concatenate([self._screech_tail, self._rng.standard_normal(frames)])
        self._screech_tail = padded[-(NOISE_TAPS - 1) :]
        slip = np.convolve(padded, self._screech_kernel, mode="valid")

        # Grab-and-release stutter, two detuned modulators beating so it rolls
        # rather than ticking. Shallower the harder the corner: at the edge of
        # grip the squeal chatters, at full slip it hardens into a howl.
        depth = 0.65 - 0.35 * self._screech_smooth
        phase_a = self._screech_judder_phase_a + 2.0 * np.pi * 29.0 * steps
        phase_b = self._screech_judder_phase_b + 2.0 * np.pi * (29.0 * 1.83) * steps
        self._screech_judder_phase_a = float(phase_a[-1] % (2.0 * np.pi))
        self._screech_judder_phase_b = float(phase_b[-1] % (2.0 * np.pi))
        stutter = 1.0 - depth * 0.5 * (1.0 + 0.62 * np.sin(phase_a) + 0.38 * np.sin(phase_b))

        return (0.8 * tone + 0.45 * slip) * stutter * gain

    def _growl_noise(self, frames: int) -> np.ndarray:
        """Band-limited strain noise, phase-continuous across blocks."""
        history = len(self._growl_kernel) - 1
        padded = np.concatenate([self._growl_tail, self._rng.standard_normal(frames)])
        self._growl_tail = padded[-history:]
        return np.convolve(padded, self._growl_kernel, mode="valid") * self.cfg.growl_noise_level

    def _rolling_noise(self, frames: int, speed_frac: float) -> np.ndarray:
        """Bearings and wheels on the floor. Keeps the whine from sounding like
        a bare oscillator."""
        # Carry the kernel's worth of history across the block boundary so the
        # filtered noise is continuous rather than restarting each callback.
        padded = np.concatenate([self._noise_tail, self._rng.standard_normal(frames)])
        self._noise_tail = padded[-(NOISE_TAPS - 1) :]
        band_limited = np.convolve(padded, self._noise_kernel, mode="valid")
        return band_limited * self.cfg.rolling_level * (0.25 + 0.75 * speed_frac)

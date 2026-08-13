# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Alternative acceleration voices for MARS -- the sound is a costume, not a fact.

``motor_synth`` models what the robot physically is: an electric motor, whining
because its poles pass and its gears mesh. These voices drop that premise. A
MARS going "brrrrrr" in a human mouth is not pretending to be a motor, and that
is the point -- once the sound stops claiming to be mechanical, nobody can hear
it as a *wrong* motor, and it is free to be charming instead.

Every voice answers the same question the motor does -- how fast is the robot
going, and how hard is it working -- and each answers in a different register:

Made of mouths:

* ``lip_trill``   a mouth doing engine noises. Buzz, lip flutter, vowel.
* ``mars_voice``  the robot's own voice, flapped into the trill it cannot say.
* ``mars_song``   that same clip struck as notes, singing up a scale.
* ``kazoo``       a hum with a membrane rattling against it.
* ``whistle``     someone whistling as they zoom past.
* ``choir``       a tiny choir going "ah", rising with the robot.
* ``throat``      khoomei: one drone, one whistled overtone climbing it.

Swiss Army Man, both halves:

* ``jetski``      propulsion by flatulence, sputtering then settling.
* ``acappella``   the all-mouth score that makes the above sincere.

Musical, where speed is tempo and pitch:

* ``music_box``   a pentatonic ladder that climbs and quickens.
* ``harp``        the same idea poured -- glissando runs that reset and climb.
* ``disco``       four on the floor, BPM tied to road speed.
* ``chiptune``    an 8-bit racer, pulse wave and arpeggio.
* ``bagpipe``     a fixed drone with a chanter climbing over it.
* ``gamelan``     bronze, every note struck twice so the pair beats.

Impossible, because a robot with a top speed has no honest top note:

* ``shepard``     a tone that rises forever and never gets any higher.
* ``risset``      the same illusion in time -- a beat that never arrives.
* ``doppler``     a real flypast, modelled: it passes you, over and over.

Speed borrowed from another domain entirely:

* ``tape``        a tape transport spooling up, wow and flutter settling.
* ``spoke_card``  a playing card in the spokes, flapping into a buzz.
* ``geiger``      randomly spaced clicks. Not higher, just more alarming.

Alive:

* ``panting``     the robot pants harder the faster you push it.
* ``heartbeat``   its pulse is your speed -- 57 bpm parked, 192 flat out.

Not synthesised at all -- generated as sound effects, then cut into something
road speed can drive (see ``scripts/fetch_sound_assets.py``):

* ``gallop``      hoofbeats fired at a rate, with a limp so it is not a metronome.
* ``quack``       a duck, increasingly concerned.
* ``boing``       a cartoon spring. No defence offered.
* ``didgeridoo``  a circular-breathing drone, revved.
* ``whale``       a humpback, pitched up until the speaker can carry it.
* ``f1``          a real race engine -- the control condition.
* ``crowd``       a stadium, delighted, carried by level rather than pitch.

Creatures and machines:

* ``purr``        a large cat, pleased about the speed.
* ``steam_train`` chuffs that quicken, alternating cylinders, a whistle on top.
* ``theremin``    a hand wavering near an aerial, sliding into every pitch.
* ``hyperdrive``  detuned saws opening up, the polite spaceship.

They share the motor's *feel* by construction: the same underdamped spring
(:class:`~mars_control.motor_synth.Rotor`) lags the heard speed behind the real
one, so pulling away swoops and stopping glides down whichever costume is on.
What differs is only how that one number is voiced.

Each voice satisfies the same interface ``motor_sound`` already drives --
``set_drive`` / ``render`` / ``trigger_startup`` plus ``enabled`` and
``volume`` -- so swapping one in is a constructor change and nothing else.
Everything is phase-continuous across blocks and allocates only per block,
because ``render`` runs on the PortAudio thread.
"""

from __future__ import annotations

import math
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import numpy as np

from mars_control.motor_synth import Rotor, Stream, bandpass_kernel, clamp01

MAX_HARMONICS = 32
"""Ceiling on additive partials. A 78 Hz buzz would otherwise ask for 60+ of
them to reach the top of the band, for partials nobody can pick out anyway."""


@dataclass(frozen=True)
class VoiceFeel:
    """How road speed becomes sound, for every voice alike.

    These are the motor's driving dynamics with the motor removed: the spring
    that makes the heard speed lag the real one, the resting level, and the
    length of the wake-up. Pitch and timbre belong to the individual voices.
    """

    max_speed: float = 0.8
    """Road speed in m/s that counts as flat out. Matches Mad mode's cap."""
    spring_hz: float = 1.2
    damping: float = 0.55
    """Below 1 the response overshoots -- the swoop on pulling away and the
    glide down when stopping. Shared with the motor so every costume moves the
    same way underneath."""
    idle_level: float = 0.12
    """How loud a voice sits when stopped but enabled."""
    startup_seconds: float = 0.9
    """Length of the wake-up fade played by ``trigger_startup()``."""
    volume: float = 0.5


@dataclass(frozen=True)
class Drive:
    """The block's driving state, handed to a voice to sing about."""

    frames: int
    dt: float
    steps: np.ndarray
    """Seconds elapsed within this block, for phase-continuous LFOs."""
    speed_frac: float
    """Road speed as a fraction of ``max_speed``, after the spring."""
    load: float
    """Throttle demand 0..1 -- what separates accelerating from cruising."""
    startup: float
    """0..1 through the wake-up; 1.0 once awake."""


@dataclass
class Glide:
    """A parameter swept across a block rather than stepped, so a fast change
    does not zipper. Every voice sweeps its pitch through one of these."""

    value: float

    def to(self, target: float, frames: int) -> np.ndarray:
        ramp = np.linspace(self.value, target, frames)
        self.value = target
        return ramp


def _phase(start: float, freq: np.ndarray, sample_rate: int) -> tuple[np.ndarray, float]:
    """Phase ramp for a per-sample frequency, plus the wrapped value to carry
    into the next block."""
    phase = start + 2.0 * np.pi * np.cumsum(freq) / sample_rate
    return phase, float(phase[-1] % (2.0 * np.pi))


def _saw(phase: np.ndarray, harmonics: int) -> np.ndarray:
    """Band-limited sawtooth. Summed rather than wrapped, because a wrapped
    ramp aliases and this runs at pitches whose harmonics reach the top of the
    band."""
    out = np.zeros(len(phase))
    for n in range(1, harmonics + 1):
        out += np.sin(n * phase) / n
    return out * (2.0 / math.pi)


def _pulse(phase: np.ndarray, harmonics: int, duty: float) -> np.ndarray:
    """Band-limited pulse wave. Duty is what gives a chiptune lead its nasal
    character -- 0.5 is a square, 0.25 is the classic NES voice."""
    out = np.full(len(phase), 2.0 * duty - 1.0)
    for n in range(1, harmonics + 1):
        out += (4.0 / (n * math.pi)) * math.sin(n * math.pi * duty) * np.cos(n * phase)
    return out


Vowel = tuple[tuple[float, float, float], ...]
"""Formants as (centre Hz, bandwidth Hz, weight)."""


def _resonance(freqs: np.ndarray, vowel: Vowel) -> np.ndarray:
    """Magnitude of a formant bank at the given frequencies."""
    gain = np.zeros_like(freqs)
    for hz, width, weight in vowel:
        gain += weight / (1.0 + ((freqs - hz) / (width / 2.0)) ** 2)
    return gain


def _voiced(phase: np.ndarray, freq: np.ndarray, harmonics: int, vowel: Vowel) -> np.ndarray:
    """A buzz shaped by a vowel -- source-filter synthesis, which is what makes
    a stack of harmonics read as a mouth rather than an organ.

    The formants weight each harmonic as it is summed rather than filtering the
    finished buzz. An FIR long enough to actually resolve a 120 Hz-wide formant
    at 48 kHz needs ~1000 taps; evaluating the same response per partial is
    both sharper and far cheaper, and tracks the pitch sweep with no zipper
    because the weight is computed per sample.
    """
    out = np.zeros(len(phase))
    for n in range(1, harmonics + 1):
        out += (_resonance(n * freq, vowel) / n) * np.sin(n * phase)
    return out


def _cycle(phase: np.ndarray) -> np.ndarray:
    """Position within each turn of a phase ramp: 0 at the strike, rising to 1.

    Turns any oscillator into a repeating envelope generator, which is how the
    chuffing, sputtering and four-on-the-floor voices get a rhythm without
    keeping a pool of events in flight.
    """
    return (phase / (2.0 * np.pi)) % 1.0


@dataclass
class Metronome:
    """Fires a whole number of events per block at a rate that may change every
    block, banking the remainder so the average rate stays exact."""

    clock: float = 0.0

    def tick(self, rate_hz: float, dt: float) -> int:
        self.clock += rate_hz * dt
        fired = int(self.clock)
        self.clock -= fired
        return fired


class PluckBank:
    """A pool of struck, decaying notes rendered together.

    Every note is generated from its absolute age rather than an advancing
    phase, so where the block boundaries happen to fall across it changes
    nothing about how it sounds.
    """

    def __init__(self, sample_rate: int, partials: tuple[tuple[float, float, float], ...], life: float = 1.6) -> None:
        self._rate = sample_rate
        self._partials = partials
        self._life = life
        self._notes: list[tuple[float, int, float]] = []

    def strike(self, freq: float, amp: float = 1.0) -> None:
        # A ceiling on simultaneous notes, not a musical decision: at the top
        # of a glissando the strike rate outruns the decay.
        if len(self._notes) >= 32:
            self._notes.pop(0)
        self._notes.append((freq, 0, amp))

    def clear(self) -> None:
        self._notes.clear()

    def render(self, frames: int) -> np.ndarray:
        out = np.zeros(frames)
        span = np.arange(frames)
        alive: list[tuple[float, int, float]] = []
        for freq, age, amp in self._notes:
            time = (age + span) / self._rate
            for ratio, amplitude, decay in self._partials:
                out += amp * amplitude * np.exp(-decay * time) * np.sin(2.0 * np.pi * freq * ratio * time)
            if age + frames < self._life * self._rate:
                alive.append((freq, age + frames, amp))
        self._notes = alive
        return out


def _bell(position: np.ndarray) -> np.ndarray:
    """Raised cosine over 0..1, zero at both ends.

    The whole trick behind the endless voices: a partial that is silent when it
    is born and silent when it dies can be recycled forever, and the ear never
    catches the join.
    """
    return 0.5 * (1.0 - np.cos(2.0 * np.pi * position))


class Transients:
    """Short bursts written at arbitrary sample offsets, carrying whatever
    overhangs into the next block.

    Needed by the voices whose events are not periodic -- a Poisson click can
    land two samples before the end of a block, and truncating it there is an
    audible tick of its own.
    """

    def __init__(self, tail: int) -> None:
        self._tail = tail
        self._pending = np.zeros(tail)

    def render(self, frames: int, bursts: list[tuple[int, np.ndarray]]) -> np.ndarray:
        buffer = np.zeros(frames + self._tail)
        buffer[: self._tail] += self._pending
        for offset, shape in bursts:
            end = min(offset + len(shape), len(buffer))
            buffer[offset:end] += shape[: end - offset]
        self._pending = buffer[frames:]
        return buffer[:frames]


_LOOPS: dict[tuple[str, int], np.ndarray | None] = {}


def load_loop(name: str, sample_rate: int) -> np.ndarray | None:
    """A shipped loop, resampled to the audio rate and peak-normalised.

    Cached because two voices share the one clip, and because reading it on the
    audio thread's first block would be a disk hit inside the callback.
    """
    key = (name, sample_rate)
    if key in _LOOPS:
        return _LOOPS[key]

    path = asset_path(name)
    if path is None:
        _LOOPS[key] = None
        return None
    decoded = load_wav(path)
    if decoded is None:
        _LOOPS[key] = None
        return None
    clip, clip_rate = decoded
    # Resample once at load, so playback rate means pitch and only pitch.
    if clip_rate != sample_rate:
        source = np.arange(len(clip)) / clip_rate
        target = np.arange(int(len(clip) * sample_rate / clip_rate)) / sample_rate
        clip = np.interp(target, source, clip)
    peak = float(np.abs(clip).max())
    _LOOPS[key] = clip / peak if peak > 1e-6 else clip
    return _LOOPS[key]


def _read_once(sample: np.ndarray, index: np.ndarray) -> np.ndarray:
    """Linearly interpolated read that stops at the end instead of wrapping --
    a one-shot that wrapped would stutter rather than finish."""
    inside = index < len(sample) - 1
    floor = np.clip(np.floor(index), 0, len(sample) - 2).astype(np.int64)
    frac = index - floor
    return inside * (sample[floor] * (1.0 - frac) + sample[floor + 1] * frac)


def _read_loop(loop: np.ndarray, index: np.ndarray) -> np.ndarray:
    """Linearly interpolated read, wrapping. The assets are cut to whole pitch
    periods and crossfaded, so a plain wrap is seamless."""
    length = len(loop)
    position = index % length
    floor = np.floor(position).astype(np.int64)
    frac = position - floor
    return loop[floor] * (1.0 - frac) + loop[(floor + 1) % length] * frac


def asset_path(name: str) -> Path | None:
    """Locate a downloaded clip, or ``None`` if this robot never fetched it.

    They sit beside the boot sounds in ``config/sounds/``, not in the package,
    because ``post_update.sh`` downloads them and downloads do not belong in a
    source tree. ``$INNATE_OS_ROOT`` is how every other node finds the repo.
    """
    root = Path(os.environ.get("INNATE_OS_ROOT", Path.home() / "innate-os"))
    path = root / "config" / "sounds" / "motor_voices" / name
    return path if path.is_file() else None


def load_wav(path: Path) -> tuple[np.ndarray, int] | None:
    """Mono float samples in [-1, 1] plus the file's sample rate, or ``None``
    for a bit depth this decoder does not speak."""
    with wave.open(str(path), "rb") as handle:
        # int16 only: any other width decodes as garbage and is then
        # peak-normalised to full scale. A missing clip beats a screaming one.
        if handle.getsampwidth() != 2:
            return None
        rate = handle.getframerate()
        channels = handle.getnchannels()
        raw = handle.readframes(handle.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


class AccelVoice:
    """Base for the acceleration voices: owns the spring, the wake-up and the
    output gain, and leaves the actual singing to ``_voice``."""

    slug: ClassVar[str] = ""
    label: ClassVar[str] = ""
    blurb: ClassVar[str] = ""
    trim: ClassVar[float] = 1.0
    """Loudness match against the motor, measured over a full drive cycle.
    Without it a saw stack lands ~6x hotter than a whistle and choosing a
    voice would mean choosing a volume.

    Matched on RMS, except for the voices made of sparse clicks. Those have
    two to three times the motor's crest factor -- 13 against 4.2 for the card
    in the spokes -- so against a shared peak ceiling they simply cannot carry
    a drone's average, and asking them to only drives them far enough into the
    saturator to flatten the clicks into blobs. They are set to the same peak
    instead and are genuinely quieter; raise ``volume`` if you pick one.
    """

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        self.feel = feel or VoiceFeel()
        self.sample_rate = sample_rate
        self.volume = self.feel.volume
        self.enabled = True

        self._speed = 0.0
        self._throttle = 0.0
        self._turn = 0.0

        self._rotor = Rotor(self.feel.spring_hz, self.feel.damping)
        self._rng = np.random.default_rng(seed)
        self._startup_elapsed = self.feel.startup_seconds
        self._gain = 0.0
        self._understudy: AccelVoice | None = None
        """Stands in when a sampled voice's asset is missing; set by the
        subclasses that load one."""

    def set_drive(self, speed: float, throttle: float, turn: float = 0.0) -> None:
        """Latest road speed (m/s), throttle demand (0..1) and yaw rate
        (rad/s, sign ignored)."""
        self._speed = speed
        self._throttle = throttle
        self._turn = turn

    def trigger_startup(self) -> None:
        """Play the wake-up: the voice fading in from nothing."""
        self._startup_elapsed = 0.0

    def rotor_speed(self) -> float:
        return self._rotor.speed

    def render(self, frames: int) -> np.ndarray:
        """Render ``frames`` mono samples as float32 in roughly [-1, 1]."""
        feel = self.feel
        dt = frames / self.sample_rate
        speed = self._rotor.update(abs(self._speed), dt)
        load = clamp01(self._throttle)
        speed_frac = clamp01(speed / max(feel.max_speed, 1e-6))

        startup = 1.0
        if self._startup_elapsed < feel.startup_seconds:
            self._startup_elapsed += dt
            startup = min(self._startup_elapsed / feel.startup_seconds, 1.0)

        level = feel.idle_level + (1.0 - feel.idle_level) * min(speed_frac * 1.4, 1.0)
        target_gain = (self.volume if self.enabled else 0.0) * level * (0.75 + 0.25 * load)
        target_gain *= max(startup, 0.05) ** 0.7

        if target_gain <= 1e-5 and self._gain <= 1e-5:
            self._quiet(dt)
            self._gain = 0.0
            return np.zeros(frames, dtype=np.float32)

        drive = Drive(
            frames=frames,
            dt=dt,
            steps=np.arange(1, frames + 1) / self.sample_rate,
            speed_frac=speed_frac,
            load=load,
            startup=startup,
        )
        signal = self._voice(drive) * self.trim
        gain = np.linspace(self._gain, target_gain, frames)
        self._gain = target_gain
        return (np.tanh(signal * gain * 1.6) * 0.85).astype(np.float32)

    def _voice(self, drive: Drive) -> np.ndarray:
        raise NotImplementedError

    def _understudy_voice(self, drive: Drive) -> np.ndarray:
        """The understudy's block, pre-scaled so the caller's trim nets out to
        the understudy's own -- the stand-in must be exactly as loud as it is
        when chosen directly."""
        if self._understudy is None:
            return np.zeros(drive.frames)
        return self._understudy._voice(drive) * (self._understudy.trim / self.trim)

    def _quiet(self, dt: float) -> None:
        """Advance whatever must keep running while silent, so resuming does
        not jump. Most voices are stateless enough not to care."""

    def _noise(self, frames: int) -> np.ndarray:
        return self._rng.standard_normal(frames)


class LipTrill(AccelVoice):
    """A human mouth doing engine noises: brrrrrrrrr.

    Source-filter, exactly as a real one works. A buzz stands in for the voice,
    a deep amplitude modulation at 20-34 Hz is the lips flapping, and a vowel
    filter is the mouth around them. The trill rate rising with speed is what
    sells it -- lips physically flutter faster when you blow harder.
    """

    slug = "lip_trill"
    label = "Lip Trill"
    blurb = "brrrrrrrr — a human mouth doing engine noises"
    trim = 1.39

    F0 = (88.0, 178.0)
    TRILL = (19.0, 34.0)
    VOWEL: Vowel = ((330.0, 120.0, 1.0), (900.0, 170.0, 0.55), (2350.0, 280.0, 0.16))
    """A slack, lip-rounded schwa -- where a mouth sits when it is flapping
    rather than saying anything."""

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.F0[0])
        self._buzz_phase = 0.0
        self._trill_phase = 0.0
        self._wobble_phase = 0.0
        self._breath = Stream(bandpass_kernel(sample_rate, 700.0, 3000.0))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.F0
        freq = self._pitch.to(low + (high - low) * drive.speed_frac, drive.frames)

        wobble = self._wobble_phase + 2.0 * np.pi * 4.5 * drive.steps
        self._wobble_phase = float(wobble[-1] % (2.0 * np.pi))
        freq = freq * (1.0 + 0.012 * np.sin(wobble))

        harmonics = min(MAX_HARMONICS, max(4, int(4200.0 / max(freq[0], 1.0))))
        buzz_phase, self._buzz_phase = _phase(self._buzz_phase, freq, self.sample_rate)
        voiced = _voiced(buzz_phase, freq, harmonics, self.VOWEL)

        trill_low, trill_high = self.TRILL
        trill_hz = trill_low + (trill_high - trill_low) * drive.speed_frac
        trill = self._trill_phase + 2.0 * np.pi * trill_hz * drive.steps
        self._trill_phase = float(trill[-1] % (2.0 * np.pi))
        # A lip is closed briefly and open for most of the cycle, so the
        # modulator leans on its own second harmonic rather than being a sine.
        flap = 0.5 * (1.0 + 0.82 * np.sin(trill) + 0.18 * np.sin(2.0 * trill))
        voiced *= 0.12 + 0.88 * flap**1.3

        breath = self._breath(self._noise(drive.frames)) * (0.05 + 0.10 * drive.load)
        return 1.7 * voiced + breath * flap


class MarsVoice(AccelVoice):
    """The robot's own voice, revving itself.

    The trill is built, not bought. TTS will not sustain a "brrrrr" -- ask it
    and you get a consonant and a short vowel -- so the asset is the longest
    steadily voiced stretch the robot's voice does hold, an "rrr" around
    150 Hz, and the flapping is done here at 19-34 Hz. That is what a lip trill
    physically is anyway: a steady voiced source chopped by the lips.

    On top of that the loop is played faster as the robot speeds up, which
    moves pitch and formants together, so at redline it is audibly the same
    robot doing an impression of itself. That is the joke, so it is not
    corrected for.

    Falls back to :class:`LipTrill` if the clip is missing, so a workspace whose
    assets were never fetched still makes a mouth noise rather than silence.
    """

    slug = "mars_voice"
    label = "MARS Voice"
    blurb = "the robot's own voice, flapped into a brrrrr"
    trim = 2.28

    ASSET = "mars_voice_brr.wav"
    RATE = (0.82, 1.85)
    TRILL = (19.0, 34.0)

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._speed_glide = Glide(self.RATE[0])
        self._cursor = 0.0
        self._trill_phase = 0.0
        self._loop = load_loop(self.ASSET, sample_rate)
        self._understudy = LipTrill(sample_rate, feel, seed) if self._loop is None else None

    def _voice(self, drive: Drive) -> np.ndarray:
        if self._loop is None:
            return self._understudy_voice(drive)

        low, high = self.RATE
        rate = self._speed_glide.to(low + (high - low) * drive.speed_frac, drive.frames)
        cursor = self._cursor + np.cumsum(rate)
        self._cursor = float(cursor[-1] % len(self._loop))
        voiced = _read_loop(self._loop, cursor)

        trill_low, trill_high = self.TRILL
        trill_hz = trill_low + (trill_high - trill_low) * drive.speed_frac
        trill = self._trill_phase + 2.0 * np.pi * trill_hz * drive.steps
        self._trill_phase = float(trill[-1] % (2.0 * np.pi))
        flap = 0.5 * (1.0 + 0.82 * np.sin(trill) + 0.18 * np.sin(2.0 * trill))
        return 1.45 * voiced * (0.14 + 0.86 * flap**1.3)


class Whistle(AccelVoice):
    """Someone whistling as they zoom past.

    Nearly a sine, because a whistle nearly is one -- the character is all in
    the breath around it and the vibrato that grows as the note climbs.
    """

    slug = "whistle"
    label = "Whistle"
    blurb = "a cheerful whistle climbing as you go"
    trim = 0.69

    F0 = (520.0, 1250.0)

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.F0[0])
        self._tone_phase = 0.0
        self._vibrato_phase = 0.0
        self._air = Stream(bandpass_kernel(sample_rate, 900.0, 4000.0))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.F0
        freq = self._pitch.to(low + (high - low) * drive.speed_frac, drive.frames)

        vibrato = self._vibrato_phase + 2.0 * np.pi * 5.2 * drive.steps
        self._vibrato_phase = float(vibrato[-1] % (2.0 * np.pi))
        freq = freq * (1.0 + (0.008 + 0.012 * drive.speed_frac) * np.sin(vibrato))

        phase, self._tone_phase = _phase(self._tone_phase, freq, self.sample_rate)
        tone = np.sin(phase) + 0.10 * np.sin(2.0 * phase)
        breath = self._air(self._noise(drive.frames)) * (0.05 + 0.05 * drive.load)
        return 0.85 * tone + breath


class MusicBox(AccelVoice):
    """A pentatonic ladder that climbs and quickens.

    Speed is told twice over: the notes rise through the scale and they arrive
    faster. Pentatonic because no two of its notes can clash, so the robot
    cannot play a wrong one however it is driven.
    """

    slug = "music_box"
    label = "Music Box"
    blurb = "pentatonic plinks that climb and quicken"
    trim = 0.92

    SCALE = (0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24, 26)
    ROOT_HZ = 392.0
    RATE = (2.2, 11.0)
    PARTIALS = ((1.0, 1.0, 3.2), (2.0, 0.5, 4.6), (3.94, 0.22, 7.0), (5.4, 0.10, 9.0))
    """(ratio, amplitude, decay 1/s) -- inharmonic and fast-decaying up top is
    what separates a struck bell from a plucked string."""

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._metronome = Metronome()
        self._bank = PluckBank(sample_rate, self.PARTIALS)
        self._step = 0

    def _quiet(self, dt: float) -> None:
        self._bank.clear()

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.RATE
        rate = low + (high - low) * drive.speed_frac
        for _ in range(self._metronome.tick(rate, drive.dt)):
            self._bank.strike(self._next_hz(drive.speed_frac))
        return 0.5 * self._bank.render(drive.frames)

    def _next_hz(self, speed_frac: float) -> float:
        """Walk up the scale with speed, ornamented so a held speed still plays
        a little figure instead of repeating one note."""
        base = speed_frac * (len(self.SCALE) - 4)
        ornament = (0, 2, 1, 3)[self._step % 4]
        self._step += 1
        semitones = self.SCALE[min(int(base) + ornament, len(self.SCALE) - 1)]
        return self.ROOT_HZ * 2.0 ** (semitones / 12.0)


class Chiptune(AccelVoice):
    """An 8-bit racer: pulse wave, arpeggio, noise channel.

    The arpeggio is the period trick -- one channel alternating between root
    and fifth fast enough to read as a chord, and jittering the rate with speed
    is how those engines sounded like they were straining.
    """

    slug = "chiptune"
    label = "Chiptune Racer"
    blurb = "8-bit engine, pulse wave and arpeggio"
    trim = 0.96

    F0 = (110.0, 300.0)
    ARP_HZ = (11.0, 26.0)

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.F0[0])
        self._lead_phase = 0.0
        self._arp_clock = 0.0
        self._arp_step = 0
        self._grit = Stream(bandpass_kernel(sample_rate, 400.0, 2600.0))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.F0
        root = low + (high - low) * drive.speed_frac

        arp_low, arp_high = self.ARP_HZ
        self._arp_clock += (arp_low + (arp_high - arp_low) * drive.speed_frac) * drive.dt
        while self._arp_clock >= 1.0:
            self._arp_clock -= 1.0
            self._arp_step += 1
        interval = (0.0, 7.0, 12.0)[self._arp_step % 3]

        freq = self._pitch.to(root * 2.0 ** (interval / 12.0), drive.frames)
        harmonics = min(MAX_HARMONICS, max(4, int(5000.0 / max(freq[0], 1.0))))
        phase, self._lead_phase = _phase(self._lead_phase, freq, self.sample_rate)
        duty = 0.22 + 0.16 * drive.speed_frac
        lead = _pulse(phase, harmonics, duty)

        noise = self._grit(self._noise(drive.frames)) * (0.06 + 0.10 * drive.speed_frac)
        return 0.42 * lead + noise


class Purr(AccelVoice):
    """A large cat, pleased about the speed.

    A purr's own frequency is around 25 Hz, far below anything a 2 W speaker
    can radiate, so it is delivered the way the motor delivers rumble: as deep
    amplitude modulation of a band that the speaker *can* reproduce.
    """

    slug = "purr"
    label = "Cat Purr"
    blurb = "a big happy cat, purring faster as you go"
    trim = 0.56

    PURR_HZ = (21.0, 33.0)
    BAND = (180.0, 900.0)

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._body = Stream(bandpass_kernel(sample_rate, *self.BAND, taps=127))
        self._purr_phase = 0.0
        self._tone_phase = 0.0
        self._pitch = Glide(200.0)

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.PURR_HZ
        purr_hz = low + (high - low) * drive.speed_frac
        purr = self._purr_phase + 2.0 * np.pi * purr_hz * drive.steps
        self._purr_phase = float(purr[-1] % (2.0 * np.pi))
        # Asymmetric: a purr is a rolling in-and-out, not a tremolo.
        cycle = 0.5 * (1.0 + np.sin(purr))
        throb = 0.18 + 0.82 * cycle**1.8

        freq = self._pitch.to(200.0 + 90.0 * drive.speed_frac, drive.frames)
        phase, self._tone_phase = _phase(self._tone_phase, freq, self.sample_rate)
        chest = 0.35 * np.sin(phase) + 0.12 * np.sin(2.0 * phase)
        rasp = self._body(self._noise(drive.frames)) * 0.9
        return 1.5 * (rasp + chest) * throb


class Hyperdrive(AccelVoice):
    """Detuned saws opening up -- the polite spaceship.

    Five saws a few cents apart beat against each other, which is the whole
    sound; the filter is faked by growing the harmonic count with speed, so
    the timbre opens as it climbs without a filter in the signal path.
    """

    slug = "hyperdrive"
    label = "Hyperdrive"
    blurb = "smooth sci-fi swell, detuned and wide"
    trim = 2.12

    F0 = (95.0, 260.0)
    DETUNE = (-0.09, -0.04, 0.0, 0.05, 0.10)
    """Semitone offsets. Uneven on purpose: evenly spaced voices beat in step
    and sound like a chorus pedal instead of a swarm."""

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.F0[0])
        self._phases = np.zeros(len(self.DETUNE))
        self._shimmer_phase = 0.0

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.F0
        freq = self._pitch.to(low + (high - low) * drive.speed_frac, drive.frames)
        harmonics = min(MAX_HARMONICS, max(3, int(4.0 + 22.0 * drive.speed_frac)))

        out = np.zeros(drive.frames)
        for index, offset in enumerate(self.DETUNE):
            voice_hz = freq * 2.0 ** (offset / 12.0)
            phase, self._phases[index] = _phase(float(self._phases[index]), voice_hz, self.sample_rate)
            out += _saw(phase, harmonics)
        out /= len(self.DETUNE)

        shimmer = self._shimmer_phase + 2.0 * np.pi * 0.7 * drive.steps
        self._shimmer_phase = float(shimmer[-1] % (2.0 * np.pi))
        return 0.9 * out * (0.88 + 0.12 * np.sin(shimmer))


class Choir(AccelVoice):
    """A tiny choir going "ah", rising with the robot.

    The same source-filter trick as the lip trill on a wider vowel, sung by
    three slightly detuned voices so it reads as a group rather than a synth.
    """

    slug = "choir"
    label = "Choir Ah"
    blurb = "a tiny choir going aaah as you accelerate"
    trim = 1.75

    F0 = (160.0, 330.0)
    DETUNE = (-0.06, 0.0, 0.07)
    VOWEL: Vowel = ((700.0, 140.0, 1.0), (1220.0, 200.0, 0.62), (2600.0, 320.0, 0.20))
    """An open "ah" -- the vowel a choir holds."""

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.F0[0])
        self._phases = np.zeros(len(self.DETUNE))
        self._vibrato_phase = 0.0
        self._breath = Stream(bandpass_kernel(sample_rate, 1200.0, 4000.0))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.F0
        freq = self._pitch.to(low + (high - low) * drive.speed_frac, drive.frames)

        vibrato = self._vibrato_phase + 2.0 * np.pi * 5.0 * drive.steps
        self._vibrato_phase = float(vibrato[-1] % (2.0 * np.pi))
        freq = freq * (1.0 + 0.014 * np.sin(vibrato))

        harmonics = min(MAX_HARMONICS, max(4, int(4500.0 / max(freq[0], 1.0))))
        sung = np.zeros(drive.frames)
        for index, offset in enumerate(self.DETUNE):
            voice_hz = freq * 2.0 ** (offset / 12.0)
            phase, self._phases[index] = _phase(float(self._phases[index]), voice_hz, self.sample_rate)
            sung += _voiced(phase, voice_hz, harmonics, self.VOWEL)
        sung /= len(self.DETUNE)

        breath = self._breath(self._noise(drive.frames)) * 0.05
        return 1.9 * sung + breath


class JetSki(AccelVoice):
    """Swiss Army Man: propulsion by flatulence.

    Structurally it is the lip trill's rude cousin -- a low voiced buzz chopped
    by a deep modulator -- but the sputter is the point. At idle the pulses are
    slow and their depth wanders, so it fires unevenly; as speed rises they
    crowd together and even out into the flat drone of an outboard motor. That
    transition from splutter to drive *is* the acceleration cue.
    """

    slug = "jetski"
    label = "Jet Ski"
    blurb = "Swiss Army Man. Propulsion by flatulence, and it does accelerate"
    trim = 1.04

    F0 = (54.0, 104.0)
    SPUTTER = (7.0, 27.0)
    VOWEL: Vowel = ((250.0, 150.0, 1.0), (620.0, 240.0, 0.45), (1500.0, 400.0, 0.10))

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.F0[0])
        self._buzz_phase = 0.0
        self._sputter_phase = 0.0
        self._wet = Stream(bandpass_kernel(sample_rate, 150.0, 900.0, taps=127))
        self._wobble = 0.0

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.F0
        freq = self._pitch.to(low + (high - low) * drive.speed_frac, drive.frames)
        harmonics = min(MAX_HARMONICS, max(6, int(2600.0 / max(freq[0], 1.0))))
        buzz_phase, self._buzz_phase = _phase(self._buzz_phase, freq, self.sample_rate)
        body = _voiced(buzz_phase, freq, harmonics, self.VOWEL)

        sputter_low, sputter_high = self.SPUTTER
        rate = sputter_low + (sputter_high - sputter_low) * drive.speed_frac
        sputter, self._sputter_phase = _phase(self._sputter_phase, np.full(drive.frames, rate), self.sample_rate)

        # The unevenness dies away with speed: a labouring engine hunts, one at
        # full chat does not.
        self._wobble += (float(self._rng.standard_normal()) - self._wobble) * 0.25
        depth = (0.92 - 0.30 * drive.speed_frac) * (1.0 + 0.22 * self._wobble * (1.0 - drive.speed_frac))
        burst = np.exp(-_cycle(sputter) * (7.0 - 4.5 * drive.speed_frac))
        gate = 1.0 - depth + depth * burst

        wet = self._wet(self._noise(drive.frames)) * (0.35 + 0.25 * drive.load)
        return 1.25 * (body + wet) * gate


class Acappella(AccelVoice):
    """Swiss Army Man's other half: the score, sung by mouths.

    That film's music is voices -- humming, chanting, breathing -- and nothing
    else, which is why a corpse-powered jet ski plays as sincere rather than
    merely gross. Three hummed voices hold a chord here, pulsed on a beat that
    quickens with speed and stepping through a small motif, so the robot
    accelerates by singing faster rather than louder.
    """

    slug = "acappella"
    label = "A Cappella"
    blurb = "Swiss Army Man's score: three mouths humming you up to speed"
    trim = 1.87

    F0 = (150.0, 300.0)
    BEAT = (1.7, 4.6)
    MOTIF = (0, 7, 12, 7, 5, 12)
    DETUNE = (-0.05, 0.0, 0.06)
    VOWEL: Vowel = ((300.0, 130.0, 1.0), (1150.0, 220.0, 0.34), (2500.0, 340.0, 0.07))
    """A closed hum: "mm", not "ah" -- the mouth shut, which is what makes it
    read as someone humming rather than a synthesiser holding a chord."""

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.F0[0])
        self._phases = np.zeros(len(self.DETUNE))
        self._beat_phase = 0.0
        self._metronome = Metronome()
        self._step = 0
        self._breath = Stream(bandpass_kernel(sample_rate, 900.0, 3500.0))

    def _voice(self, drive: Drive) -> np.ndarray:
        beat_low, beat_high = self.BEAT
        beat_hz = beat_low + (beat_high - beat_low) * drive.speed_frac
        for _ in range(self._metronome.tick(beat_hz, drive.dt)):
            self._step += 1

        low, high = self.F0
        root = low + (high - low) * drive.speed_frac
        semitones = self.MOTIF[self._step % len(self.MOTIF)]
        freq = self._pitch.to(root * 2.0 ** (semitones / 12.0), drive.frames)

        harmonics = min(MAX_HARMONICS, max(4, int(4000.0 / max(freq[0], 1.0))))
        sung = np.zeros(drive.frames)
        for index, offset in enumerate(self.DETUNE):
            voice_hz = freq * 2.0 ** (offset / 12.0)
            phase, self._phases[index] = _phase(float(self._phases[index]), voice_hz, self.sample_rate)
            sung += _voiced(phase, voice_hz, harmonics, self.VOWEL)
        sung /= len(self.DETUNE)

        beat, self._beat_phase = _phase(self._beat_phase, np.full(drive.frames, beat_hz), self.sample_rate)
        position = _cycle(beat)
        swell = np.minimum(position * 22.0, 1.0) * np.exp(-2.4 * position)
        breath = self._breath(self._noise(drive.frames)) * 0.05 * swell
        return 2.6 * sung * (0.10 + 0.90 * swell) + breath


class SteamTrain(AccelVoice):
    """A little steam engine, chuffing faster as it goes.

    Chuff rate is the speedometer, and it is the one engine sound everybody can
    already read without being told. Two cylinders fire alternately, so every
    other chuff is slightly softer and darker -- get that wrong and it sounds
    like a machine rather than a locomotive.
    """

    slug = "steam_train"
    label = "Steam Train"
    blurb = "chuff-chuff, quicker and quicker, with a whistle at the top"
    trim = 1.05

    CHUFF = (1.8, 9.0)

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._chuff_phase = 0.0
        self._steam = Stream(bandpass_kernel(sample_rate, 300.0, 2400.0, taps=127))
        self._rumble = Stream(bandpass_kernel(sample_rate, 170.0, 420.0, taps=127))
        self._whistle_phase = 0.0

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.CHUFF
        rate = low + (high - low) * drive.speed_frac
        chuff, self._chuff_phase = _phase(self._chuff_phase, np.full(drive.frames, rate), self.sample_rate)

        position = _cycle(chuff)
        burst = np.exp(-position * 11.0)
        # Half-rate square: the alternating cylinder, quieter on its stroke.
        cylinder = np.where(_cycle(chuff / 2.0) < 0.5, 1.0, 0.72)

        steam = self._steam(self._noise(drive.frames)) * burst * cylinder
        rumble = self._rumble(self._noise(drive.frames)) * (0.25 + 0.5 * drive.speed_frac)

        whistle = 0.0
        if drive.speed_frac > 0.82:
            lean = (drive.speed_frac - 0.82) / 0.18
            phase, self._whistle_phase = _phase(
                self._whistle_phase, np.full(drive.frames, 620.0 + 40.0 * drive.speed_frac), self.sample_rate
            )
            whistle = lean * 0.16 * (np.sin(phase) + 0.4 * np.sin(1.5 * phase))
        return 1.5 * steam + 0.5 * rumble + whistle


class Disco(AccelVoice):
    """Four on the floor, and the tempo is the speedometer.

    Everyone already knows how to hear a groove speeding up, so tying beats per
    minute to metres per second needs no explaining. It is the only voice here
    where the robot is not making a noise so much as playing you a track.
    """

    slug = "disco"
    label = "Disco"
    blurb = "a four-on-the-floor groove whose tempo is your speed"
    trim = 2.99

    BEAT = (1.7, 4.4)
    BASSLINE = (0, 0, 7, 5, 0, 0, 3, 7)
    ROOT_HZ = 65.4

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._beat_phase = 0.0
        self._metronome = Metronome()
        self._step = 0
        self._kick_phase = 0.0
        self._bass_phase = 0.0
        self._hat = Stream(bandpass_kernel(sample_rate, 2600.0, 6000.0))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.BEAT
        beat_hz = low + (high - low) * drive.speed_frac
        for _ in range(self._metronome.tick(beat_hz, drive.dt)):
            self._step += 1

        beat, self._beat_phase = _phase(self._beat_phase, np.full(drive.frames, beat_hz), self.sample_rate)
        position = _cycle(beat)

        # The kick's pitch drops through its own decay -- that fall is what the
        # ear reads as weight, and it costs one envelope.
        thump = np.exp(-position * 16.0)
        kick_hz = 62.0 + 78.0 * thump
        kick_phase, self._kick_phase = _phase(self._kick_phase, kick_hz * np.ones(drive.frames), self.sample_rate)
        kick = np.sin(kick_phase) * thump

        offbeat = np.exp(-((_cycle(beat + np.pi) ** 0.5) * 14.0))
        hat = self._hat(self._noise(drive.frames)) * offbeat * 0.30

        semitones = self.BASSLINE[self._step % len(self.BASSLINE)]
        bass_hz = self.ROOT_HZ * 2.0 ** (semitones / 12.0) * 2.0
        bass_phase, self._bass_phase = _phase(self._bass_phase, bass_hz * np.ones(drive.frames), self.sample_rate)
        bass = _pulse(bass_phase, 12, 0.32) * np.exp(-position * 3.2) * 0.30

        return 1.15 * kick + bass + hat


class Theremin(AccelVoice):
    """A hand wavering near an aerial.

    Almost a sine, like the whistle, but the opposite temperament: it slides
    into every pitch instead of arriving, and the vibrato is wide and slow
    enough to sound like an unsteady hand rather than an effect.
    """

    slug = "theremin"
    label = "Theremin"
    blurb = "a wavering sci-fi swoop, sliding up to speed"
    trim = 0.67

    F0 = (190.0, 690.0)

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.F0[0])
        self._slide = self.F0[0]
        self._tone_phase = 0.0
        self._vibrato_phase = 0.0
        self._drift = 0.0

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.F0
        # A slower glide than the other voices use: on a theremin the slide
        # between notes is the instrument, not a transition between them. The
        # slide is tracked separately from the Glide, which still has to ramp
        # within the block -- smooth them both with one variable and every
        # block starts where it ends, which is a stair, not a slide.
        target = low + (high - low) * drive.speed_frac
        self._slide += (target - self._slide) * min(drive.dt / 0.22, 1.0)
        freq = self._pitch.to(self._slide, drive.frames)

        self._drift += (float(self._rng.standard_normal()) * 0.4 - self._drift) * 0.08
        vibrato = self._vibrato_phase + 2.0 * np.pi * (5.6 + 0.6 * self._drift) * drive.steps
        self._vibrato_phase = float(vibrato[-1] % (2.0 * np.pi))
        freq = freq * (1.0 + 0.028 * np.sin(vibrato) + 0.004 * self._drift)

        phase, self._tone_phase = _phase(self._tone_phase, freq, self.sample_rate)
        tone = np.sin(phase) + 0.16 * np.sin(2.0 * phase) + 0.05 * np.sin(3.0 * phase)
        return 0.9 * tone * (0.93 + 0.07 * np.sin(vibrato * 0.5))


class Bagpipe(AccelVoice):
    """A drone that never moves and a chanter that never stops.

    The drone is fixed, so the climbing chanter has something to be measured
    against -- speed reads as the interval between them widening. Absurd on a
    robot, which is the entire recommendation.
    """

    slug = "bagpipe"
    label = "Bagpipe"
    blurb = "a fixed drone and a chanter climbing over it. Yes, really"
    trim = 0.69

    DRONE_HZ = (116.5, 233.0)
    SCALE = (0, 2, 4, 5, 7, 9, 10, 12, 14, 16)
    CHANTER_HZ = 233.0
    NOTE = (2.4, 8.0)
    REED: Vowel = ((450.0, 260.0, 1.0), (1300.0, 420.0, 0.5), (2400.0, 500.0, 0.16))

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._drone_phases = np.zeros(len(self.DRONE_HZ))
        self._chanter_phase = 0.0
        self._pitch = Glide(self.CHANTER_HZ)
        self._metronome = Metronome()
        self._step = 0

    def _voice(self, drive: Drive) -> np.ndarray:
        drone = np.zeros(drive.frames)
        for index, hz in enumerate(self.DRONE_HZ):
            phase, self._drone_phases[index] = _phase(
                float(self._drone_phases[index]), np.full(drive.frames, hz), self.sample_rate
            )
            drone += _voiced(phase, np.full(drive.frames, hz), 20, self.REED)
        drone /= len(self.DRONE_HZ)

        low, high = self.NOTE
        for _ in range(self._metronome.tick(low + (high - low) * drive.speed_frac, drive.dt)):
            self._step += 1

        top = drive.speed_frac * (len(self.SCALE) - 3)
        grace = (0, 2, 1, 0)[self._step % 4]
        semitones = self.SCALE[min(int(top) + grace, len(self.SCALE) - 1)]
        freq = self._pitch.to(self.CHANTER_HZ * 2.0 ** (semitones / 12.0), drive.frames)
        harmonics = min(MAX_HARMONICS, max(4, int(3800.0 / max(freq[0], 1.0))))
        phase, self._chanter_phase = _phase(self._chanter_phase, freq, self.sample_rate)
        chanter = _voiced(phase, freq, harmonics, self.REED)

        return 1.6 * (0.42 * drone + 0.58 * chanter)


class Kazoo(AccelVoice):
    """A membrane buzzing against a hum.

    A kazoo does not make a sound of its own -- it takes a voice and rattles a
    membrane against it, which multiplies the harmonics and makes anything
    hummed into it ridiculous. Modelled the same way: a hum, then a fast
    shallow rattle on top.
    """

    slug = "kazoo"
    label = "Kazoo"
    blurb = "the sound of a person taking this very seriously"
    trim = 0.67

    F0 = (165.0, 380.0)
    NASAL: Vowel = ((380.0, 150.0, 0.7), (1750.0, 320.0, 1.0), (2600.0, 400.0, 0.30))
    """Weighted onto the second formant, which is the nasal honk that says
    kazoo. The top is capped well below where it would start to shriek."""

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.F0[0])
        self._buzz_phase = 0.0
        self._rattle_phase = 0.0

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.F0
        freq = self._pitch.to(low + (high - low) * drive.speed_frac, drive.frames)
        harmonics = min(MAX_HARMONICS, max(4, int(3400.0 / max(freq[0], 1.0))))
        phase, self._buzz_phase = _phase(self._buzz_phase, freq, self.sample_rate)
        hum = _voiced(phase, freq, harmonics, self.NASAL)

        # The membrane runs at its own pitch, not the hum's, so the two beat
        # against each other -- that beat is the buzz you actually hear.
        rattle, self._rattle_phase = _phase(self._rattle_phase, freq * 2.02, self.sample_rate)
        return 2.0 * hum * (0.72 + 0.28 * np.sin(rattle))


class Harp(AccelVoice):
    """Glissando runs that get faster and climb higher.

    The music box strikes one note at a time; this pours them. Same pentatonic
    reasoning -- nothing can clash -- but with a long decay, so at speed the
    notes overlap into a wash rather than a sequence.
    """

    slug = "harp"
    label = "Harp"
    blurb = "cascading glissando runs, pouring faster as you speed up"
    trim = 0.74

    SCALE = (0, 2, 4, 7, 9)
    ROOT_HZ = 261.6
    RATE = (4.0, 19.0)
    OCTAVES = 3
    PARTIALS = ((1.0, 1.0, 1.4), (2.0, 0.42, 2.2), (3.0, 0.20, 3.1), (4.0, 0.09, 4.4))

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._metronome = Metronome()
        self._bank = PluckBank(sample_rate, self.PARTIALS, life=2.4)
        self._step = 0

    def _quiet(self, dt: float) -> None:
        self._bank.clear()

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.RATE
        rate = low + (high - low) * drive.speed_frac
        steps = len(self.SCALE) * self.OCTAVES
        for _ in range(self._metronome.tick(rate, drive.dt)):
            # A run that restarts low each time it tops out, so the ear keeps
            # getting the rising gesture instead of one endless climb.
            position = self._step % steps
            semitones = self.SCALE[position % len(self.SCALE)] + 12 * (position // len(self.SCALE))
            self._bank.strike(self.ROOT_HZ * 2.0 ** (semitones / 12.0), amp=0.55 + 0.45 * (1.0 - position / steps))
            self._step += 1
        return 0.42 * self._bank.render(drive.frames)


class MarsSong(AccelVoice):
    """The robot's voice, but singing.

    Same clip as :class:`MarsVoice`, resampled per note instead of held: the
    robot goes "bo bo bo" up a pentatonic scale, faster and higher as it
    accelerates. It keeps its own formants as the pitch moves, so it gets
    steadily more excited about the whole thing.
    """

    slug = "mars_song"
    label = "MARS Sings"
    blurb = "the robot's voice going bo-bo-bo up a scale"
    trim = 1.5

    ASSET = "mars_voice_brr.wav"
    SCALE = (0, 3, 5, 7, 10, 12)
    """One octave, and no more: resampling lifts the formants along with the
    pitch, so every extra octave is also a doubling of how bright the robot
    gets. Two octaves put more energy above 2.5 kHz than anything else here."""
    RATE = (2.4, 10.0)
    LIFE = 0.42

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._loop = load_loop(self.ASSET, sample_rate)
        self._understudy = MusicBox(sample_rate, feel, seed) if self._loop is None else None
        self._metronome = Metronome()
        self._notes: list[tuple[float, int]] = []
        self._step = 0

    def _quiet(self, dt: float) -> None:
        self._notes.clear()

    def _voice(self, drive: Drive) -> np.ndarray:
        if self._loop is None:
            return self._understudy_voice(drive)

        low, high = self.RATE
        for _ in range(self._metronome.tick(low + (high - low) * drive.speed_frac, drive.dt)):
            step = drive.speed_frac * (len(self.SCALE) - 2)
            ornament = (0, 1, 0, 2)[self._step % 4]
            semitones = self.SCALE[min(int(step) + ornament, len(self.SCALE) - 1)]
            self._notes.append((2.0 ** (semitones / 12.0), 0))
            self._step += 1

        out = np.zeros(drive.frames)
        span = np.arange(drive.frames)
        alive: list[tuple[float, int]] = []
        for rate, age in self._notes:
            time = (age + span) / self.sample_rate
            envelope = np.minimum(time * 90.0, 1.0) * np.exp(-time * 6.5)
            out += _read_loop(self._loop, (age + span) * rate) * envelope
            if age + drive.frames < self.LIFE * self.sample_rate:
                alive.append((rate, age + drive.frames))
        self._notes = alive
        return 1.5 * out


class Shepard(AccelVoice):
    """A tone that rises forever and never gets any higher.

    Five sines an octave apart climb together through a fixed window; each one
    fades in at the bottom of the span and out at the top, so when it is reborn
    an octave down nobody hears the join. The pitch appears to rise without
    limit while the spectrum never actually moves.

    Which makes it the one honest answer to a robot that has a top speed: hold
    the throttle down and it keeps accelerating, forever, and the sound never
    runs out of room. Speed sets how fast it climbs, not how high it has got.
    """

    slug = "shepard"
    label = "Infinite Riser"
    blurb = "a Shepard tone: it accelerates forever and never gets higher"
    trim = 0.88

    SPAN = 5
    """Octaves across the window. With one partial per octave, the set is
    identical after each wrap, which is what makes the illusion perfect."""
    BASE_HZ = 55.0
    RATE = (0.05, 0.42)
    """Full traversals of the span per second -- never zero, so even parked it
    is still, faintly, getting away from you."""

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._theta = 0.0
        self._phases = np.zeros(self.SPAN)

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.RATE
        rate = low + (high - low) * drive.speed_frac
        theta = self._theta + rate * drive.steps
        self._theta = float(theta[-1] % 1.0)

        out = np.zeros(drive.frames)
        for k in range(self.SPAN):
            position = (theta + k / self.SPAN) % 1.0
            freq = self.BASE_HZ * 2.0 ** (position * self.SPAN)
            phase, self._phases[k] = _phase(float(self._phases[k]), freq, self.sample_rate)
            # The octave jump when position wraps happens under a zero window,
            # so the discontinuity in this partial is never audible.
            out += _bell(position) * np.sin(phase)
        return 0.5 * out


class Risset(AccelVoice):
    """The same illusion in time: a groove that speeds up forever.

    Four layers of clicks at octave-related tempos, each fading in slow at the
    bottom and out fast at the top. The beat accelerates without limit and the
    average tempo never changes -- a staircase you can run up in one place.
    """

    slug = "risset"
    label = "Infinite Groove"
    blurb = "a Risset rhythm: the beat speeds up forever and never arrives"
    trim = 2.6

    LAYERS = 4
    SPAN = 4
    SLOWEST_HZ = 0.85
    RATE = (0.03, 0.26)

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._theta = 0.0
        self._beats = np.zeros(self.LAYERS)
        self._tones = np.zeros(self.LAYERS)
        self._crack = Stream(bandpass_kernel(sample_rate, 400.0, 2800.0))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.RATE
        rate = low + (high - low) * drive.speed_frac
        theta = self._theta + rate * drive.steps
        self._theta = float(theta[-1] % 1.0)

        noise = self._crack(self._noise(drive.frames))
        out = np.zeros(drive.frames)
        for k in range(self.LAYERS):
            position = (theta + k / self.LAYERS) % 1.0
            tempo = self.SLOWEST_HZ * 2.0 ** (position * self.SPAN)
            beat, self._beats[k] = _phase(float(self._beats[k]), tempo, self.sample_rate)
            # Pitch and decay ride the layer's own tempo, so a layer thumps low
            # while it is slow and ticks high once it is fast.
            tone_hz = 110.0 * 2.0 ** (position * 2.2)
            tone, self._tones[k] = _phase(float(self._tones[k]), tone_hz, self.sample_rate)
            strike = np.exp(-_cycle(beat) * (11.0 + 34.0 * position))
            out += _bell(position) * strike * (np.sin(tone) + 0.45 * noise)
        return 0.55 * out


class Doppler(AccelVoice):
    """The robot sounds like it is overtaking you. Repeatedly.

    A real pass is modelled, not faked: a source goes by at a fixed miss
    distance, and what you hear is its pitch divided by one plus the radial
    velocity over the speed of sound, with loudness falling off as one over the
    distance. The eeeee-yowwww swoop and its loudness are then guaranteed to
    agree, which is the half that faked versions get wrong.

    A real MARS at 0.8 m/s shifts pitch by a fifth of a percent, so the speed
    of sound here is a fiction chosen to make the effect audible. Road speed
    sets how often it passes.
    """

    slug = "doppler"
    label = "Doppler Pass"
    blurb = "it flies past you, over and over, and never arrives"
    trim = 0.72

    PASS_RATE = (0.3, 1.5)
    SOURCE_HZ = 235.0
    MACH = 0.55
    """Source speed as a fraction of this world's speed of sound. Above about
    0.7 the swoop stops sounding like a pass and starts sounding broken."""
    SPREAD = 3.6
    """How many miss-distances either side of the listener a pass covers."""

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pass_phase = 0.0
        self._tone_phase = 0.0
        self._wind = Stream(bandpass_kernel(sample_rate, 400.0, 3000.0))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.PASS_RATE
        rate = low + (high - low) * drive.speed_frac
        passing, self._pass_phase = _phase(self._pass_phase, np.full(drive.frames, rate), self.sample_rate)

        along = (_cycle(passing) * 2.0 - 1.0) * self.SPREAD
        distance = np.sqrt(along**2 + 1.0)
        heard = 1.0 / (1.0 + self.MACH * along / distance)
        loudness = 1.0 / distance

        freq = self.SOURCE_HZ * heard
        phase, self._tone_phase = _phase(self._tone_phase, freq, self.sample_rate)
        # Fewer partials than the source really has: distance eats the top of a
        # spectrum, and loudness alone does not read as far away.
        tone = _saw(phase, 14) * loudness
        wind = self._wind(self._noise(drive.frames)) * loudness**1.8 * 0.5
        return 1.5 * (tone + wind)


class Tape(AccelVoice):
    """The robot as a tape machine, spooling up.

    Wow and flutter are the whole idea: a transport wavers most when it is
    slow, and steadies as it comes up to speed. So the sound gets *more*
    stable the faster the robot goes, which is the opposite of every other
    voice here and reads, unmistakably, as a machine finding its feet.
    """

    slug = "tape"
    label = "Tape Spool"
    blurb = "a tape transport coming up to speed, wow and flutter settling"
    trim = 0.59

    CAPSTAN = (150.0, 640.0)
    WOW_HZ = 0.9
    FLUTTER_HZ = 16.0

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.CAPSTAN[0])
        self._whine_phase = 0.0
        self._wow_phase = 0.0
        self._flutter_phase = 0.0
        self._hiss = Stream(bandpass_kernel(sample_rate, 1200.0, 6500.0))
        self._reel = Stream(bandpass_kernel(sample_rate, 160.0, 430.0, taps=127))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.CAPSTAN
        freq = self._pitch.to(low + (high - low) * drive.speed_frac, drive.frames)

        steadiness = 1.0 - 0.75 * drive.speed_frac
        wow = self._wow_phase + 2.0 * np.pi * self.WOW_HZ * drive.steps
        flutter = self._flutter_phase + 2.0 * np.pi * self.FLUTTER_HZ * drive.steps
        self._wow_phase = float(wow[-1] % (2.0 * np.pi))
        self._flutter_phase = float(flutter[-1] % (2.0 * np.pi))
        freq = freq * (1.0 + steadiness * (0.045 * np.sin(wow) + 0.011 * np.sin(flutter)))

        phase, self._whine_phase = _phase(self._whine_phase, freq, self.sample_rate)
        whine = _saw(phase, 9) * 0.55
        hiss = self._hiss(self._noise(drive.frames)) * 0.16
        reel = self._reel(self._noise(drive.frames)) * (0.2 + 0.5 * drive.speed_frac)
        return 1.3 * whine + hiss + reel


class SpokeCard(AccelVoice):
    """A playing card pegged into the spokes.

    The one sound on this list that a small wheeled machine has an actual claim
    to, and everybody already knows how it maps to speed because they made one
    as a child. Slow, it is a flap; fast, the flaps fuse into a buzz, and that
    fusion happening on its own is the acceleration.
    """

    slug = "spoke_card"
    label = "Card in the Spokes"
    blurb = "a playing card pegged into the wheel, flapping into a buzz"
    trim = 0.8

    RATE = (4.0, 27.0)
    BODY_HZ = 780.0

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._flap_phase = 0.0
        self._body_phase = 0.0
        self._slap = Stream(bandpass_kernel(sample_rate, 380.0, 2600.0))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.RATE
        rate = low + (high - low) * drive.speed_frac
        flap, self._flap_phase = _phase(self._flap_phase, np.full(drive.frames, rate), self.sample_rate)
        strike = np.exp(-_cycle(flap) * 38.0)

        body, self._body_phase = _phase(self._body_phase, np.full(drive.frames, self.BODY_HZ), self.sample_rate)
        card = self._slap(self._noise(drive.frames)) + 0.45 * np.sin(body)
        return 2.1 * card * strike


class Geiger(AccelVoice):
    """Click rate as speed, and the clicks land at random.

    Everything else here is periodic, so it is the only voice whose texture
    changes rather than its pitch: the ear reads a Poisson process by density
    alone, and going faster makes the robot sound more dangerous rather than
    higher. Randomly spaced clicks are also the only ones that never form a
    tone, however fast they get.
    """

    slug = "geiger"
    label = "Geiger Counter"
    blurb = "randomly spaced clicks. Faster is not higher, just more alarming"
    trim = 1.2

    RATE = (2.5, 52.0)
    SHAPES = 5
    TICK_SECONDS = 0.008

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        length = int(self.TICK_SECONDS * sample_rate)
        time = np.arange(length) / sample_rate
        # Raw white noise in a click puts most of its energy in the top octave,
        # which is the harshest thing this whole set can produce; the short
        # window rolls that off and leaves the body of the tick.
        window = np.hanning(9)
        window /= window.sum()
        # A handful of ticks rather than one: a counter whose every click is
        # bit-identical reads as a sample being retriggered, not as a detector.
        self._ticks = [
            np.exp(-time * 480.0)
            * (
                np.convolve(self._rng.standard_normal(length), window, mode="same") * 0.45
                + np.sin(2.0 * np.pi * hz * time)
            )
            for hz in np.linspace(1050.0, 2050.0, self.SHAPES)
        ]
        self._transients = Transients(length)

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.RATE
        rate = low + (high - low) * drive.speed_frac
        count = int(self._rng.poisson(rate * drive.dt))
        bursts = [
            (int(self._rng.integers(0, drive.frames)), self._ticks[int(self._rng.integers(0, self.SHAPES))])
            for _ in range(count)
        ]
        return 1.4 * self._transients.render(drive.frames, bursts)


class Panting(AccelVoice):
    """The robot is out of breath, and getting worse.

    Effort, not velocity: the breath comes faster, and a voice creeps into the
    out-breath as it works harder, the way a person's does. It is the only
    voice here that sounds like the robot would rather you slowed down.
    """

    slug = "panting"
    label = "Out of Breath"
    blurb = "the robot pants harder the faster you push it"
    trim = 0.61

    RATE = (1.1, 4.6)
    F0 = (150.0, 205.0)
    MOUTH: Vowel = ((600.0, 320.0, 1.0), (1500.0, 500.0, 0.5), (2600.0, 600.0, 0.18))

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._breath_phase = 0.0
        self._voice_phase = 0.0
        self._pitch = Glide(self.F0[0])
        self._air = Stream(bandpass_kernel(sample_rate, 380.0, 2500.0))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.RATE
        rate = low + (high - low) * drive.speed_frac
        breath, self._breath_phase = _phase(self._breath_phase, np.full(drive.frames, rate), self.sample_rate)
        position = _cycle(breath)

        # Out on the first half, in on the second and quieter -- a pant is not
        # symmetric, and swapping the two makes it sound like a sigh.
        out_breath = np.sin(np.pi * np.clip(position / 0.45, 0.0, 1.0)) ** 1.4
        in_breath = 0.55 * np.sin(np.pi * np.clip((position - 0.5) / 0.5, 0.0, 1.0)) ** 1.6
        air = self._air(self._noise(drive.frames)) * (out_breath + in_breath)

        f_low, f_high = self.F0
        freq = self._pitch.to(f_low + (f_high - f_low) * drive.speed_frac, drive.frames)
        phase, self._voice_phase = _phase(self._voice_phase, freq, self.sample_rate)
        harmonics = min(MAX_HARMONICS, max(4, int(3200.0 / max(freq[0], 1.0))))
        voiced = _voiced(phase, freq, harmonics, self.MOUTH) * out_breath * (0.15 + 0.85 * drive.speed_frac)
        return 1.5 * air + 1.1 * voiced


class Throat(AccelVoice):
    """Khoomei: one throat holding a drone and whistling a melody above it.

    The drone never moves. What climbs is a single very narrow resonance
    sliding up through its harmonics, picking them out one at a time, so the
    melody is not a second voice -- it is the same voice, filtered. Speed
    chooses which harmonic is singing.
    """

    slug = "throat"
    label = "Throat Singing"
    blurb = "one drone, and a whistling overtone climbing through its harmonics"
    trim = 0.91

    F0 = (92.0, 116.0)
    WHISTLE = (480.0, 2050.0)
    WHISTLE_WIDTH = 52.0
    """Narrow enough to isolate one harmonic at a time -- widen it and the
    overtone stops being a whistle and becomes a vowel."""

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._pitch = Glide(self.F0[0])
        self._drone_phase = 0.0

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.F0
        freq = self._pitch.to(low + (high - low) * drive.speed_frac, drive.frames)
        whistle_low, whistle_high = self.WHISTLE
        whistle_hz = whistle_low + (whistle_high - whistle_low) * drive.speed_frac

        vowel: Vowel = ((330.0, 260.0, 0.55), (whistle_hz, self.WHISTLE_WIDTH, 2.4))
        phase, self._drone_phase = _phase(self._drone_phase, freq, self.sample_rate)
        harmonics = min(MAX_HARMONICS, max(8, int(2600.0 / max(freq[0], 1.0))))
        return 2.2 * _voiced(phase, freq, harmonics, vowel)


class Gamelan(AccelVoice):
    """Bronze, and two of everything.

    Gamelan instruments are tuned in pairs a few hertz apart so the pair beats
    against itself -- ombak, the wave. Every note here is struck twice for that
    reason, and the partials are deliberately not whole-number multiples,
    because struck bronze is not.
    """

    slug = "gamelan"
    label = "Gamelan"
    blurb = "shimmering bronze, every note struck twice so the pair beats"
    trim = 1.34

    SCALE = (0.0, 2.4, 4.8, 7.2, 9.6, 12.0, 14.4, 16.8)
    """Roughly equidistant steps, after slendro -- which is not a subset of the
    twelve-tone scale and sounds wrong quantised into one."""
    ROOT_HZ = 330.0
    OMBAK = 0.007
    RATE = (1.4, 6.2)
    PARTIALS = ((1.0, 1.0, 1.1), (2.76, 0.55, 1.8), (5.4, 0.25, 2.6), (8.93, 0.12, 3.4))

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._metronome = Metronome()
        self._bank = PluckBank(sample_rate, self.PARTIALS, life=3.0)
        self._step = 0

    def _quiet(self, dt: float) -> None:
        self._bank.clear()

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.RATE
        for _ in range(self._metronome.tick(low + (high - low) * drive.speed_frac, drive.dt)):
            top = drive.speed_frac * (len(self.SCALE) - 3)
            ornament = (0, 2, 1, 3)[self._step % 4]
            semitones = self.SCALE[min(int(top) + ornament, len(self.SCALE) - 1)]
            freq = self.ROOT_HZ * 2.0 ** (semitones / 12.0)
            self._bank.strike(freq, amp=0.5)
            self._bank.strike(freq * (1.0 + self.OMBAK), amp=0.5)
            self._step += 1
        return 0.42 * self._bank.render(drive.frames)


class Heartbeat(AccelVoice):
    """Lub-dub, and the robot's pulse is the speedometer.

    A real heart is around 50 Hz, well under what the speaker can radiate, so
    this one is pitched up until it can be heard and given the falling pitch
    that makes a thump read as weight rather than a beep.
    """

    slug = "heartbeat"
    label = "Heartbeat"
    blurb = "its pulse is your speed. 57 bpm parked, 192 flat out"
    trim = 1.4

    RATE = (0.95, 3.2)
    DUB_AT = 0.32

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._beat_phase = 0.0
        self._thump_phase = 0.0
        self._body = Stream(bandpass_kernel(sample_rate, 170.0, 700.0, taps=127))

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.RATE
        rate = low + (high - low) * drive.speed_frac
        beat, self._beat_phase = _phase(self._beat_phase, np.full(drive.frames, rate), self.sample_rate)

        lub = np.exp(-_cycle(beat) * 17.0)
        dub = 0.68 * np.exp(-_cycle(beat - 2.0 * np.pi * self.DUB_AT) * 23.0)
        envelope = lub + dub

        freq = 96.0 + 105.0 * envelope
        phase, self._thump_phase = _phase(self._thump_phase, freq, self.sample_rate)
        thud = self._body(self._noise(drive.frames)) * envelope * 0.45
        return 1.7 * (np.sin(phase) * envelope + thud)


class LoopVoice(AccelVoice):
    """A sampled loop, played faster as the robot speeds up.

    For material that is not worth synthesising -- a whale, a crowd, a real
    engine -- because an additive model of it reads as a cartoon of the thing
    while a recording is simply correct. Resampling moves pitch and timbre
    together, which is exactly what a real thing speeding up does.

    Subclasses supply an asset and a rate range; the assets are cut to whole
    pitch periods and crossfaded by ``scripts/fetch_sound_assets.py``, so the
    wrap needs no windowing here.
    """

    ASSET: ClassVar[str] = ""
    RATE: ClassVar[tuple[float, float]] = (0.9, 1.8)
    UNDERSTUDY: ClassVar[type[AccelVoice]] = Hyperdrive
    """Stands in when the asset is missing, so a workspace whose assets were
    never fetched makes a noise rather than silence."""

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._cursor = 0.0
        self._speed_glide = Glide(self.RATE[0])
        self._loop = load_loop(self.ASSET, sample_rate)
        self._understudy = self.UNDERSTUDY(sample_rate, feel, seed) if self._loop is None else None

    def _voice(self, drive: Drive) -> np.ndarray:
        if self._loop is None:
            return self._understudy_voice(drive)
        low, high = self.RATE
        rate = self._speed_glide.to(low + (high - low) * drive.speed_frac, drive.frames)
        cursor = self._cursor + np.cumsum(rate)
        self._cursor = float(cursor[-1] % len(self._loop))
        return _read_loop(self._loop, cursor)


class TriggerVoice(AccelVoice):
    """A sampled one-shot, fired at a rate the road speed sets.

    The other half of the sampled idea: rather than stretching one sound out,
    repeat it. Speed becomes how often the thing happens, which is how a
    gallop or a quacking duck reads as going faster without anything having to
    rise in pitch.
    """

    ASSET: ClassVar[str] = ""
    RATE: ClassVar[tuple[float, float]] = (2.0, 8.0)
    PITCH: ClassVar[tuple[float, float]] = (1.0, 1.2)
    ACCENT: ClassVar[tuple[float, ...]] = (1.0,)
    """Amplitude per successive hit. More than one entry gives the pattern a
    limp, which is what separates a gallop from a metronome."""
    JITTER: ClassVar[float] = 0.03
    """Random spread on each hit's playback rate. A living thing never makes
    the same noise twice, and identical repeats read as a stuck sample."""
    UNDERSTUDY: ClassVar[type[AccelVoice]] = MusicBox

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._metronome = Metronome()
        self._hits: list[tuple[float, int, float]] = []
        self._step = 0
        self._sample = load_loop(self.ASSET, sample_rate)
        self._understudy = self.UNDERSTUDY(sample_rate, feel, seed) if self._sample is None else None

    def _quiet(self, dt: float) -> None:
        self._hits.clear()

    def _voice(self, drive: Drive) -> np.ndarray:
        if self._sample is None:
            return self._understudy_voice(drive)

        low, high = self.RATE
        for _ in range(self._metronome.tick(low + (high - low) * drive.speed_frac, drive.dt)):
            pitch_low, pitch_high = self.PITCH
            rate = pitch_low + (pitch_high - pitch_low) * drive.speed_frac
            rate *= 1.0 + self.JITTER * float(self._rng.standard_normal())
            self._hits.append((max(rate, 0.1), 0, self.ACCENT[self._step % len(self.ACCENT)]))
            self._step += 1

        out = np.zeros(drive.frames)
        span = np.arange(drive.frames)
        alive: list[tuple[float, int, float]] = []
        for rate, age, accent in self._hits:
            out += accent * _read_once(self._sample, (age + span) * rate)
            if (age + drive.frames) * rate < len(self._sample):
                alive.append((rate, age + drive.frames, accent))
        self._hits = alive
        return out


class Gallop(TriggerVoice):
    """A horse. The robot has four legs now, apparently."""

    slug = "gallop"
    label = "Gallop"
    blurb = "hoofbeats. It gets faster, not higher — and it has a limp on purpose"
    trim = 8.0
    ASSET = "gallop.wav"
    RATE = (2.2, 9.5)
    PITCH = (1.0, 1.12)
    ACCENT = (1.0, 0.55, 0.85, 0.5)


class Quack(TriggerVoice):
    """A duck, becoming steadily more concerned about the speed."""

    slug = "quack"
    label = "Duck"
    blurb = "quacking, faster and more frantic the harder you drive"
    trim = 2.19
    ASSET = "quack.wav"
    RATE = (1.5, 6.5)
    PITCH = (0.74, 1.12)
    """Pitched down, not up. A quack is nasal to begin with, and revving one
    the way the other sampled voices are revved put half its energy above
    2.5 kHz -- the brightest thing here by a factor of five. Frequency now does
    less of the work and the rate does more."""
    JITTER = 0.05


class Boing(TriggerVoice):
    """A cartoon spring. There is no defence of this one."""

    slug = "boing"
    label = "Boing"
    blurb = "a cartoon spring, bouncing quicker and higher"
    trim = 1.61
    ASSET = "boing.wav"
    RATE = (1.4, 7.0)
    PITCH = (0.85, 1.5)
    JITTER = 0.04


class Didgeridoo(LoopVoice):
    """A didgeridoo drone, revved.

    Its fundamental sits near 60 Hz, well under what the speaker can radiate,
    so the rate range starts above 1 -- this is the voice that loses most in
    the move from headphones to the robot.
    """

    slug = "didgeridoo"
    label = "Didgeridoo"
    blurb = "a circular-breathing drone, wound up as you go"
    trim = 0.79
    ASSET = "didgeridoo.wav"
    RATE = (1.15, 2.5)


class Whale(LoopVoice):
    """A humpback, played fast enough to hear on a 2 W speaker."""

    slug = "whale"
    label = "Whale Song"
    blurb = "a humpback moaning, pitched up until the speaker can carry it"
    trim = 0.73
    ASSET = "whale.wav"
    RATE = (1.5, 3.4)


class RaceCar(LoopVoice):
    """An actual Formula One engine -- the control condition.

    Worth having on the board precisely because it is the obvious answer: it
    settles whether the robot sounds better being what it is not, or being a
    small machine with a voice of its own.
    """

    slug = "f1"
    label = "Formula One"
    blurb = "a real race engine. The obvious answer, included so you can rule it out"
    trim = 1.04
    ASSET = "f1.wav"
    RATE = (0.75, 1.75)


class Crowd(LoopVoice):
    """A stadium, delighted that the robot is moving.

    Pitch is deliberately barely touched -- a resampled crowd stops being
    people -- so this one lets the shared level curve carry the speed instead.
    """

    slug = "crowd"
    label = "Crowd"
    blurb = "a stadium cheering you on, louder the faster you go"
    trim = 1.02
    ASSET = "crowd.wav"
    RATE = (0.95, 1.2)


VOICES: dict[str, type[AccelVoice]] = {
    voice.slug: voice
    for voice in (
        LipTrill,
        MarsVoice,
        MarsSong,
        Whistle,
        MusicBox,
        Harp,
        Chiptune,
        Disco,
        Purr,
        Hyperdrive,
        Theremin,
        Choir,
        Acappella,
        JetSki,
        SteamTrain,
        Bagpipe,
        Kazoo,
        Shepard,
        Risset,
        Doppler,
        Tape,
        SpokeCard,
        Geiger,
        Panting,
        Throat,
        Gamelan,
        Heartbeat,
        Gallop,
        Quack,
        Boing,
        Didgeridoo,
        Whale,
        RaceCar,
        Crowd,
    )
}


def build_voice(slug: str, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> AccelVoice:
    """Construct a voice by name. Raises ``KeyError`` for an unknown one, so a
    typo in the config surfaces as a warning and a fallback rather than a
    silent robot."""
    return VOICES[slug](sample_rate, feel, seed)

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

Swiss Army Man, both halves:

* ``jetski``      propulsion by flatulence, sputtering then settling.
* ``acappella``   the all-mouth score that makes the above sincere.

Musical, where speed is tempo and pitch:

* ``music_box``   a pentatonic ladder that climbs and quickens.
* ``harp``        the same idea poured -- glissando runs that reset and climb.
* ``disco``       four on the floor, BPM tied to road speed.
* ``chiptune``    an 8-bit racer, pulse wave and arpeggio.
* ``bagpipe``     a fixed drone with a chanter climbing over it.

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
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import numpy as np

from mars_control.motor_synth import Rotor, bandpass_kernel

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


class Stream:
    """A FIR filter that carries its tail across block boundaries, so filtered
    noise is continuous rather than restarting on every callback."""

    def __init__(self, kernel: np.ndarray) -> None:
        self._kernel = kernel
        self._tail = np.zeros(len(kernel) - 1)

    def __call__(self, block: np.ndarray) -> np.ndarray:
        padded = np.concatenate([self._tail, block])
        self._tail = padded[-(len(self._kernel) - 1) :]
        return np.convolve(padded, self._kernel, mode="valid")


def _clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


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
    clip, clip_rate = load_wav(path)
    # Resample once at load, so playback rate means pitch and only pitch.
    if clip_rate != sample_rate:
        source = np.arange(len(clip)) / clip_rate
        target = np.arange(int(len(clip) * sample_rate / clip_rate)) / sample_rate
        clip = np.interp(target, source, clip)
    peak = float(np.abs(clip).max())
    _LOOPS[key] = clip / peak if peak > 1e-6 else clip
    return _LOOPS[key]


def _read_loop(loop: np.ndarray, index: np.ndarray) -> np.ndarray:
    """Linearly interpolated read, wrapping. The assets are cut to whole pitch
    periods and crossfaded, so a plain wrap is seamless."""
    length = len(loop)
    position = index % length
    floor = np.floor(position).astype(np.int64)
    frac = position - floor
    return loop[floor] * (1.0 - frac) + loop[(floor + 1) % length] * frac


def asset_path(name: str) -> Path | None:
    """Locate a shipped audio asset, installed share directory first so a
    built workspace wins over the source tree."""
    candidates: list[Path] = []
    try:
        from ament_index_python.packages import get_package_share_directory

        candidates.append(Path(get_package_share_directory("mars_control")) / "assets" / name)
    except (ImportError, KeyError):
        pass
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / name)
    return next((path for path in candidates if path.is_file()), None)


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    """Mono float samples in [-1, 1], plus the file's sample rate."""
    with wave.open(str(path), "rb") as handle:
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
    voice would mean choosing a volume."""

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
        load = _clamp01(self._throttle)
        speed_frac = _clamp01(speed / max(feel.max_speed, 1e-6))

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

    Falls back to :class:`LipTrill` if the clip is missing, so a workspace that
    never ran the fetch script still makes a mouth noise rather than silence.
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
            return self._understudy._voice(drive) if self._understudy else np.zeros(drive.frames)

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
            return self._understudy._voice(drive) if self._understudy else np.zeros(drive.frames)

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
    )
}


def build_voice(slug: str, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> AccelVoice:
    """Construct a voice by name. Raises ``KeyError`` for an unknown one, so a
    typo in the config surfaces as a warning and a fallback rather than a
    silent robot."""
    return VOICES[slug](sample_rate, feel, seed)

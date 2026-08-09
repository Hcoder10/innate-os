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

* ``lip_trill``  a mouth doing engine noises. Buzz, lip flutter, vowel.
* ``mars_voice`` the robot's own Cartesia voice, looped and revved.
* ``whistle``    someone whistling as they zoom past.
* ``music_box``  a pentatonic ladder that climbs and quickens.
* ``chiptune``   an 8-bit racer, pulse wave and arpeggio.
* ``purr``       a large cat, pleased about the speed.
* ``hyperdrive`` detuned saws opening up, the polite spaceship.
* ``choir``      a tiny choir going "ah", rising with the robot.

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
    """The robot's own Cartesia voice, looped and revved.

    A seamless loop of MARS saying "brrrrr" is played back at a rate that rises
    with speed. Resampling moves pitch and formants together, so at redline it
    is audibly the same robot doing a chipmunk impression of itself -- which is
    the joke, and why it is not corrected for.

    Falls back to :class:`LipTrill` if the clip is missing, so a workspace that
    never ran the fetch script still makes a mouth noise rather than silence.
    """

    slug = "mars_voice"
    label = "MARS Voice"
    blurb = "the robot's own voice, revving itself"
    trim = 1.27

    ASSET = "mars_voice_brr.wav"
    RATE = (0.82, 1.85)

    def __init__(self, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> None:
        super().__init__(sample_rate, feel, seed)
        self._speed_glide = Glide(self.RATE[0])
        self._cursor = 0.0
        self._loop: np.ndarray | None = None
        self._understudy: LipTrill | None = None

        path = asset_path(self.ASSET)
        if path is None:
            self._understudy = LipTrill(sample_rate, feel, seed)
            return
        clip, clip_rate = load_wav(path)
        # Resample once at load so playback rate means pitch only, never a
        # sample-rate mismatch.
        if clip_rate != sample_rate:
            source = np.arange(len(clip)) / clip_rate
            target = np.arange(int(len(clip) * sample_rate / clip_rate)) / sample_rate
            clip = np.interp(target, source, clip)
        peak = float(np.abs(clip).max())
        self._loop = clip / peak if peak > 1e-6 else clip

    def _voice(self, drive: Drive) -> np.ndarray:
        if self._loop is None:
            return self._understudy._voice(drive) if self._understudy else np.zeros(drive.frames)

        low, high = self.RATE
        rate = self._speed_glide.to(low + (high - low) * drive.speed_frac, drive.frames)
        cursor = self._cursor + np.cumsum(rate)
        length = len(self._loop)
        self._cursor = float(cursor[-1] % length)

        # The asset is already crossfaded end-to-start, so a plain wrap is
        # seamless and the read needs no windowing.
        index = cursor % length
        floor = np.floor(index).astype(np.int64)
        frac = index - floor
        nxt = (floor + 1) % length
        return 1.15 * (self._loop[floor] * (1.0 - frac) + self._loop[nxt] * frac)


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
        self._clock = 0.0
        self._step = 0
        self._notes: list[tuple[float, int]] = []

    def _quiet(self, dt: float) -> None:
        self._notes.clear()

    def _voice(self, drive: Drive) -> np.ndarray:
        low, high = self.RATE
        rate = low + (high - low) * drive.speed_frac
        self._clock += rate * drive.dt
        while self._clock >= 1.0:
            self._clock -= 1.0
            self._notes.append((self._next_hz(drive.speed_frac), 0))

        out = np.zeros(drive.frames)
        alive: list[tuple[float, int]] = []
        for freq, age in self._notes:
            time = (age + np.arange(drive.frames)) / self.sample_rate
            for ratio, amplitude, decay in self.PARTIALS:
                out += amplitude * np.exp(-decay * time) * np.sin(2.0 * np.pi * freq * ratio * time)
            if age + drive.frames < 1.6 * self.sample_rate:
                alive.append((freq, age + drive.frames))
        self._notes = alive
        return 0.5 * out

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


VOICES: dict[str, type[AccelVoice]] = {
    voice.slug: voice for voice in (LipTrill, MarsVoice, Whistle, MusicBox, Chiptune, Purr, Hyperdrive, Choir)
}


def build_voice(slug: str, sample_rate: int = 48000, feel: VoiceFeel | None = None, seed: int = 0) -> AccelVoice:
    """Construct a voice by name. Raises ``KeyError`` for an unknown one, so a
    typo in the config surfaces as a warning and a fallback rather than a
    silent robot."""
    return VOICES[slug](sample_rate, feel, seed)

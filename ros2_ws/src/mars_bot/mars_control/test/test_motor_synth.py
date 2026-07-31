# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Tests for the motor whine synthesis.

Pure DSP with no ROS in it, so these run anywhere. They check the things the
sound actually depends on: that pitch tracks speed, that the spring overshoots
rather than tracking exactly, that nothing is wasted below the speaker's floor,
and that no block boundary clicks.
"""

import numpy as np
import pytest

from mars_control.motor_synth import MotorConfig, MotorSynth, _Gearbox, _Rotor, is_mad_scale

SAMPLE_RATE = 48000
BLOCK = 512


def render(synth, speed, throttle, blocks=200):
    synth.set_drive(speed, throttle)
    return np.concatenate([synth.render(BLOCK) for _ in range(blocks)])


def spectrum(buffer):
    magnitude = np.abs(np.fft.rfft(buffer))
    freqs = np.fft.rfftfreq(len(buffer), 1.0 / SAMPLE_RATE)
    return freqs, magnitude


def pitch_hz(buffer):
    freqs, magnitude = spectrum(buffer)
    audible = (freqs > 80) & (freqs < 1500)
    return float(freqs[audible][np.argmax(magnitude[audible])])


def test_pitch_rises_with_speed():
    """The whole 'shows speed' claim: within a gear, faster must mean higher.
    A single tall gear isolates the pitch map from the shift drops."""
    cfg = MotorConfig(gear_tops=(1.0,))
    pitches = [
        pitch_hz(render(MotorSynth(SAMPLE_RATE, cfg), cfg.max_speed * fraction, 1.0))
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    assert pitches == sorted(pitches), f"pitch must rise with speed, got {pitches}"
    assert pitches[0] == pytest.approx(cfg.base_hz, rel=0.15)
    assert pitches[-1] == pytest.approx(cfg.top_hz, rel=0.15)


def test_sweep_is_wide_enough_to_read_as_speed():
    """A narrow sweep is the difference between hearing acceleration and not.
    The stretched-recording version managed 9 semitones; synthesis has no such
    limit, so hold it to well over an octave."""
    cfg = MotorConfig()
    semitones = 12 * np.log2(cfg.top_hz / cfg.base_hz)
    assert semitones > 18


def test_spring_overshoots_on_pull_away():
    """The overshoot is the charm: it swoops instead of tracking exactly."""
    cfg = MotorConfig()
    rotor = _Rotor(cfg)
    trace = [rotor.update(cfg.max_speed, 1 / 200) for _ in range(400)]

    assert max(trace) > cfg.max_speed * 1.05, "expected the motor to overshoot the robot"
    assert trace[-1] == pytest.approx(cfg.max_speed, rel=0.02), "and then settle on it"


def test_critically_damped_spring_does_not_overshoot():
    """The overshoot has to be the damping doing it, so that turning damping
    up genuinely gives the mechanical-sounding version."""
    cfg = MotorConfig(damping=1.0)
    rotor = _Rotor(cfg)
    trace = [rotor.update(cfg.max_speed, 1 / 200) for _ in range(400)]
    assert max(trace) <= cfg.max_speed * 1.01


def test_rotor_speed_never_goes_negative():
    """Undershoot past zero would fold the pitch back up through the bottom
    of the sweep, which sounds like a bounce that is not there."""
    cfg = MotorConfig()
    rotor = _Rotor(cfg)
    for _ in range(200):
        rotor.update(cfg.max_speed, 1 / 200)
    for _ in range(400):
        assert rotor.update(0.0, 1 / 200) >= 0.0


def test_speaker_floor_is_respected():
    """The MAX98357A radiates almost nothing below ~160 Hz. Unlike an engine,
    a whine has no reason to put anything down there at all."""
    cfg = MotorConfig(gear_tops=(1.0,))
    buffer = render(MotorSynth(SAMPLE_RATE, cfg), cfg.max_speed * 0.9, 1.0)

    freqs, magnitude = spectrum(buffer)
    power = magnitude**2
    assert power[freqs < 160].sum() / power.sum() < 0.02


def test_load_changes_the_sound_at_the_same_speed():
    """Accelerating and coasting must not sound identical."""
    cfg = MotorConfig()
    speed = cfg.max_speed * 0.5
    coasting = render(MotorSynth(SAMPLE_RATE, cfg), speed, 0.0)
    pulling = render(MotorSynth(SAMPLE_RATE, cfg), speed, 1.0)
    assert np.sqrt((pulling**2).mean()) > np.sqrt((coasting**2).mean()) * 1.05


def test_gearbox_sweeps_and_drops_at_each_shift():
    """Rising speed sweeps the whine up, snaps it back at a shift, and sweeps
    again. Without the drops the pitch just ramps once and reads far weaker."""
    cfg = MotorConfig()
    gearbox = _Gearbox(cfg)

    through = []
    for step in range(400):
        position, _ = gearbox.update(step / 399, 0.01)
        through.append(position)

    drops = [i for i in range(1, len(through)) if through[i] < through[i - 1] - 0.1]
    assert len(drops) >= len(cfg.gear_tops) - 1, "expected a drop per upshift"
    assert max(through) > 0.95


def test_gear_hysteresis_prevents_chatter():
    """Speed hovering on a shift boundary must not rattle between gears."""
    cfg = MotorConfig()
    gearbox = _Gearbox(cfg)
    boundary = cfg.gear_tops[0]

    gearbox.update(boundary * 1.05, 0.01)
    settled = gearbox.gear
    for step in range(200):
        gearbox.update(boundary * (1.0 + 0.02 * np.sin(step)), 0.01)
        assert gearbox.gear == settled


def test_startup_chirp_rises_into_idle():
    """trigger_startup() sweeps the whine in from below and fades it in --
    a wake-up, not a pop to full idle."""
    cfg = MotorConfig()
    synth = MotorSynth(SAMPLE_RATE, cfg)
    synth.trigger_startup()
    synth.set_drive(0.0, 0.0)

    early = np.concatenate([synth.render(BLOCK) for _ in range(19)])  # ~0.2 s
    settled = np.concatenate([synth.render(BLOCK) for _ in range(94)])[-SAMPLE_RATE:]

    def pitch(buffer):
        freqs, magnitude = spectrum(buffer)
        audible = (freqs > 30) & (freqs < 1500)
        return float(freqs[audible][np.argmax(magnitude[audible])])

    assert pitch(early) < pitch(settled) * 0.8, "chirp should start well below idle"
    assert np.abs(early[:BLOCK]).max() < np.abs(settled).max() * 0.25, "and fade in"


def test_fresh_synth_does_not_chirp_uninvited():
    """The chirp only plays when asked; a synth built mid-session must come up
    at the idle whine, not re-run its wake-up."""
    cfg = MotorConfig()
    synth = MotorSynth(SAMPLE_RATE, cfg)
    buffer = render(synth, 0.0, 0.0, blocks=20)

    freqs, magnitude = spectrum(buffer)
    audible = (freqs > 30) & (freqs < 1500)
    assert float(freqs[audible][np.argmax(magnitude[audible])]) == pytest.approx(cfg.base_hz, rel=0.1)


def test_brush_flutter_modulates_the_envelope():
    """The brush tremolo is the 'trembly' in the sound: with it the amplitude
    envelope visibly flutters, without it the whine is steady."""

    def envelope_ratio(depth):
        cfg = MotorConfig(brush_depth=depth, gear_tops=(1.0,))
        synth = MotorSynth(SAMPLE_RATE, cfg)
        # Discard the spool-up: the slow spring sweeps pitch for ~1 s and that
        # sweep would drown the tremolo being measured.
        buffer = render(synth, cfg.max_speed * 0.5, 0.3, blocks=200)[SAMPLE_RATE:]
        smooth = np.convolve(np.abs(buffer), np.ones(192) / 192, "same")[2000:-2000]
        return smooth.std() / smooth.mean()

    assert envelope_ratio(0.5) > envelope_ratio(0.0) * 1.4


def test_vibrato_spreads_the_spectral_peak():
    """The wobble is audible as a waver; spectrally it smears the dominant
    partial. Without it the peak is a needle."""

    def concentration(depth):
        cfg = MotorConfig(wobble_depth=depth, brush_depth=0.0, gear_tops=(1.0,), rolling_level=0.0)
        synth = MotorSynth(SAMPLE_RATE, cfg)
        buffer = render(synth, cfg.max_speed * 0.5, 0.3)
        freqs, magnitude = spectrum(buffer)
        power = magnitude**2
        dominant = freqs[np.argmax(power)]
        near = (freqs > dominant * 0.995) & (freqs < dominant * 1.005)
        return power[near].sum() / power.sum()

    assert concentration(0.018) < concentration(0.0) * 0.6


def test_acceleration_growl_roughens_the_envelope():
    """The growl is load-driven rumble: flooring it must be audibly rougher
    than coasting at the same speed, and turning the growl off must remove
    most of that difference."""

    def roughness(load, depth=0.5, noise=0.20):
        cfg = MotorConfig(gear_tops=(1.0,), growl_depth=depth, growl_noise_level=noise)
        synth = MotorSynth(SAMPLE_RATE, cfg)
        # Discard the full spool-up (the slow spring takes ~1 s to settle), so
        # only the held-speed envelope is judged, not the pitch sweep.
        buffer = render(synth, cfg.max_speed * 0.5, load, blocks=300)[SAMPLE_RATE + SAMPLE_RATE // 2 :]
        smooth = np.convolve(np.abs(buffer), np.ones(240) / 240, "same")[2000:-2000]
        return smooth.std() / smooth.mean()

    assert roughness(1.0) > roughness(0.0) * 1.5, "load should growl"
    # Both halves of the feature off -- the throb AM and the strain noise --
    # must remove the load roughness entirely, back down to the coast level.
    assert roughness(1.0) > roughness(1.0, depth=0.0, noise=0.0) * 1.5, "and the growl knobs should be doing it"


def test_mad_mode_detection_from_robot_info():
    """The engine gate: exactly the payload shapes mars_app publishes, plus
    the malformed ones that must fail towards silence."""
    modes = [
        {"id": "slow", "label": "Slow", "scale": 0.35},
        {"id": "fast", "label": "Fast", "scale": 1.0},
        {"id": "mad", "label": "Mad", "scale": 2.0},
    ]

    assert is_mad_scale({"drive_speed_scale": 2.0, "drive_speed_modes": modes})
    assert not is_mad_scale({"drive_speed_scale": 1.0, "drive_speed_modes": modes})
    assert not is_mad_scale({"drive_speed_scale": 0.35, "drive_speed_modes": modes})

    # Mad retuned robot-side: the id, not the number, is what means Mad.
    retuned = [{"id": "mad", "label": "Mad", "scale": 1.6}]
    assert is_mad_scale({"drive_speed_scale": 1.6, "drive_speed_modes": retuned})
    assert not is_mad_scale({"drive_speed_scale": 2.0, "drive_speed_modes": retuned})

    # Payloads from robots predating speed modes, or plain garbage.
    assert not is_mad_scale({})
    assert not is_mad_scale({"drive_speed_scale": 2.0})
    assert not is_mad_scale({"drive_speed_scale": 2.0, "drive_speed_modes": "mad"})
    assert not is_mad_scale({"drive_speed_scale": "mad", "drive_speed_modes": modes})
    assert not is_mad_scale({"drive_speed_scale": 2.0, "drive_speed_modes": [{"id": "slow", "scale": 2.0}]})


def test_no_click_at_block_boundaries():
    """Phase, noise filter and gain all carry across blocks. A break would show
    up as a jump at the boundary that does not occur inside the blocks."""
    cfg = MotorConfig()
    synth = MotorSynth(SAMPLE_RATE, cfg)

    blocks = []
    for i in range(80):
        synth.set_drive(cfg.max_speed * i / 79, 1.0)
        blocks.append(synth.render(BLOCK))
    buffer = np.concatenate(blocks)

    steps = np.abs(np.diff(buffer))
    at_boundary = steps[BLOCK - 1 :: BLOCK]
    interior = np.delete(steps, np.arange(BLOCK - 1, len(steps), BLOCK))
    assert at_boundary.max() <= interior.max() * 1.5


def test_disabling_fades_out_without_a_click():
    cfg = MotorConfig()
    synth = MotorSynth(SAMPLE_RATE, cfg)
    synth.set_drive(cfg.max_speed * 0.9, 1.0)
    for _ in range(20):
        synth.render(BLOCK)

    synth.enabled = False
    fading = synth.render(BLOCK)
    assert np.abs(fading[-1]) < 1e-6, "should reach silence by the end of the block"
    assert np.abs(np.diff(fading)).max() < 0.5, "fade must be a ramp, not a cut"
    assert np.abs(synth.render(BLOCK)).max() == 0.0


@pytest.mark.parametrize("blocksize", [64, 128, 256, 512, 1024])
def test_output_is_bounded_and_finite_at_any_block_size(blocksize):
    cfg = MotorConfig()
    synth = MotorSynth(SAMPLE_RATE, cfg)
    synth.set_drive(cfg.max_speed, 1.0)
    buffer = np.concatenate([synth.render(blocksize) for _ in range(100)])

    assert np.isfinite(buffer).all()
    assert np.abs(buffer).max() < 1.0
    assert buffer.dtype == np.float32

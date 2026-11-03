# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The macOS software equaliser's chain: the Sonar EQ as biquads.
"""

from __future__ import annotations

import math

import pytest

np = pytest.importorskip("numpy")   # macOS only (pyproject)

from arctis_sound_manager.eq_types import EqBand, rbj_coefficients  # noqa: E402
from arctis_sound_manager.platform_support import IS_MACOS  # noqa: E402
from arctis_sound_manager.soft_eq_dsp import (SPEAKERS, Compressor, Gate,  # noqa: E402
                                              PartitionedConvolver, Renderer, Spatializer,
                                              StereoBiquads, build_chain, build_mic_chain,
                                              downmix_matrix, front_impulses, hesuvi_impulses,
                                              load_wav, plate_impulses, response_db,
                                              smart_volume_params)


def _db(band: EqBand, f: float) -> float:
    b0, b1, b2, a1, a2 = rbj_coefficients(band, 48000)
    return response_db(np.array([[b0, b1, b2, 1.0, a1, a2]]), 1.0, [f])[0]


def test_peaking_reaches_its_gain_at_its_frequency():
    band = EqBand(freq=1000, gain=6, q=1.0, type="peakingEQ")
    assert _db(band, 1000) == pytest.approx(6.0, abs=0.01)
    assert abs(_db(band, 20)) < 0.1


def test_shelves_reach_their_gain_on_their_side():
    assert _db(EqBand(freq=200, gain=-6, q=0.707, type="lowShelving"), 20) == pytest.approx(-6, abs=0.1)
    assert _db(EqBand(freq=4000, gain=5, q=0.707, type="highShelving"), 18000) == pytest.approx(5, abs=0.2)


def test_notch_and_band_pass_with_q_gain():
    assert _db(EqBand(freq=1000, q=2.0, type="notchFilter"), 1000) < -40
    assert _db(EqBand(freq=1000, q=4.0, type="bandPassPeakQ"), 1000) == pytest.approx(
        20 * math.log10(4.0), abs=0.01)


def test_bypass_has_no_filter():
    assert rbj_coefficients(EqBand(type="byPass"), 48000) is None


def test_flat_channel_is_a_plain_copy():
    sos, gain = build_chain([EqBand(gain=3, enabled=False), EqBand(gain=3, type="byPass")], 0, 0, 0)
    assert sos.shape == (0, 6) and gain == 1.0


def test_active_bands_and_the_three_macros():
    bands = [EqBand(freq=100, gain=4, q=1, type="peakingEQ"),
             EqBand(freq=5000, gain=-3, q=1, type="peakingEQ", enabled=False)]

    sos, gain = build_chain(bands, basses_db=0, voix_db=0, aigus_db=0)

    assert sos.shape == (4, 6)
    # Lowered by the largest boost, so the curve peaks at 0 dB.
    assert gain == pytest.approx(10 ** (-4 / 20))
    assert max(response_db(sos, gain, [80, 100, 120])) == pytest.approx(0.0, abs=0.3)


def test_boost_sits_on_top_of_the_headroom():
    sos, gain = build_chain([], basses_db=-2, voix_db=0, aigus_db=0, boost_db=6)
    assert gain == pytest.approx(10 ** (6 / 20))
    assert response_db(sos, gain, [20000])[0] == pytest.approx(6.0, abs=0.3)


def test_out_of_range_numbers_are_clamped():
    bands = [EqBand(freq=float("inf"), gain=float("nan"), q=0.0, type="peakingEQ"),
             EqBand(freq=40000, gain=50, q=99, type="peakingEQ")]

    sos, gain = build_chain(bands, 0, 0, 0)

    assert np.isfinite(sos).all() and math.isfinite(gain)
    assert gain == pytest.approx(10 ** (-12 / 20))


def _reference(sos, x):
    """Direct form I, one sample at a time: what the cascade must equal."""
    y = x.astype(np.float64).copy()
    for b0, b1, b2, _a0, a1, a2 in sos:
        for ch in range(y.shape[1]):
            x1 = x2 = y1 = y2 = 0.0
            for i in range(y.shape[0]):
                xn = y[i, ch]
                yn = b0 * xn + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
                x2, x1, y2, y1 = x1, xn, y1, yn
                y[i, ch] = yn
    return y


@pytest.mark.skipif(not IS_MACOS, reason="vDSP is Accelerate's, macOS only")
def test_vdsp_cascade_matches_the_reference_across_blocks():
    sos, _gain = build_chain([EqBand(freq=1000, gain=6, q=1),
                              EqBand(freq=150, gain=-4, q=0.7, type="lowShelving")], 2, 0, -1)
    rng = np.random.default_rng(0)
    x = rng.uniform(-0.5, 0.5, (1024, 2)).astype(np.float32)
    x[:, 1] *= 0.3                       # the channels must stay apart

    cascade = StereoBiquads(sos)
    y = x.copy()
    for block in np.split(y, 8):         # views: filtered in place, state carried
        cascade.process(block)
    cascade.close()

    np.testing.assert_allclose(y, _reference(sos, x), atol=2e-4)


@pytest.mark.skipif(not IS_MACOS, reason="vDSP is Accelerate's, macOS only")
def test_a_flat_cascade_leaves_the_frames_alone():
    x = np.ones((64, 2), dtype=np.float32)
    StereoBiquads(np.zeros((0, 6))).process(x)
    assert (x == 1).all()


# ── Smart Volume ──────────────────────────────────────────────────────────────

def test_smart_volume_off_or_at_zero_is_no_compressor():
    assert smart_volume_params(None) is None
    assert smart_volume_params({"enabled": False, "level": 80, "loudness": "loud"}) is None
    assert smart_volume_params({"enabled": True, "level": 0, "loudness": "loud"}) is None


def test_the_level_scales_ratio_and_makeup_as_on_linux():
    params = smart_volume_params({"enabled": True, "level": 50, "loudness": "balanced"})
    assert params["threshold"] == -18.0
    assert params["ratio"] == pytest.approx(2.5) and params["makeup"] == pytest.approx(4.0)
    # An unknown mode is Balanced, a level out of range is clamped.
    assert smart_volume_params({"enabled": True, "level": 500, "loudness": "?"})["ratio"] == 4.0


def test_the_knee_curve():
    c = Compressor({"threshold": -20, "ratio": 4, "knee": 6, "makeup": 0,
                    "attack": 5, "release": 200})
    assert c.gain_db(-40) == 0.0                         # under the knee
    assert c.gain_db(0) == pytest.approx(-15.0)          # 20 dB over at 4:1
    assert -1.0 < c.gain_db(-20) < 0.0                   # inside the knee, gently


def _tone(db: float, seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(48000 * seconds)) / 48000
    amplitude = 10 ** (db / 20) * math.sqrt(2)          # RMS = db
    return np.stack([amplitude * np.sin(2 * np.pi * 440 * t)] * 2, axis=1).astype(np.float32)


def _settled_rms_db(compressor: Compressor, x: np.ndarray) -> float:
    for start in range(0, len(x), 256):
        compressor.process(x[start:start + 256])
    tail = x[-4800:]
    return 20 * math.log10(math.sqrt(float(np.mean(np.square(tail)))))


def test_loud_sound_comes_down_and_quiet_sound_comes_up():
    params = smart_volume_params({"enabled": True, "level": 100, "loudness": "balanced"})
    # Balanced: -18 dB threshold, 4:1, +8 dB makeup.
    loud = _settled_rms_db(Compressor(params), _tone(-6))
    quiet = _settled_rms_db(Compressor(params), _tone(-40))
    assert loud == pytest.approx(-18 + 12 / 4 + 8, abs=1.0)
    assert quiet == pytest.approx(-40 + 8, abs=0.5)


def test_the_gain_never_steps_between_blocks():
    c = Compressor(smart_volume_params({"enabled": True, "level": 100, "loudness": "quiet"}))
    x = np.ones((512, 2), dtype=np.float32) * 0.5
    c.process(x[:256])
    c.process(x[256:])
    jumps = np.abs(np.diff(x[:, 0]))
    assert jumps.max() < 0.05


# ── Spatial Audio ─────────────────────────────────────────────────────────────

HRIR_DIR = __import__("pathlib").Path(__file__).parent.parent / "src" / "arctis_sound_manager" / "hrir_assets"


def test_bundled_hrirs_load_as_float():
    hrir, rate = load_wav(HRIR_DIR / "ds3d+++.wav")
    assert rate == 48000 and hrir.shape == (16800, 14) and hrir.dtype == np.float32


def test_integer_wavs_load_too(tmp_path):
    import wave
    with wave.open(str(tmp_path / "pcm.wav"), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(np.array([[16384, -16384]], dtype="<i2").tobytes())
    hrir, _ = load_wav(tmp_path / "pcm.wav")
    assert hrir.tolist() == [[0.5, -0.5]]


def test_front_impulses_follow_the_hesuvi_layouts():
    fourteen = np.arange(14, dtype=np.float32)[None, :].repeat(3, axis=0)
    front = front_impulses(fourteen)
    assert front[:, :, 0].tolist() == [[0, 1], [8, 7]]   # FL->L, FL->R, FR->L, FR->R
    seven = np.arange(7, dtype=np.float32)[None, :].repeat(3, axis=0)
    assert front_impulses(seven)[:, :, 0].tolist() == [[0, 1], [1, 0]]   # mirrored


def _run(stage, x):
    """Block by block through a Spatializer or a Renderer: the two ears."""
    return np.concatenate([stage.process(x[start:start + stage.block])
                           for start in range(0, len(x), stage.block)])


def test_every_speaker_takes_its_hesuvi_impulses():
    fourteen = np.arange(14, dtype=np.float32)[None, :].repeat(3, axis=0)
    impulses = hesuvi_impulses(fourteen)[:, :, 0].tolist()
    # FL FR FC LFE RL RR SL SR, to (left, right): as Linux's convolvers.
    assert impulses == [[0, 1], [8, 7], [6, 13], [6, 13], [4, 5], [12, 11], [2, 3], [10, 9]]
    seven = np.arange(7, dtype=np.float32)[None, :].repeat(3, axis=0)
    assert hesuvi_impulses(seven)[:, :, 0].tolist() == [
        [0, 1], [1, 0], [6, 6], [6, 6], [4, 5], [5, 4], [2, 3], [3, 2]]


def test_the_downmix_folds_7_1_as_on_linux():
    m = downmix_matrix()
    assert m[:, 0].tolist() == pytest.approx([1, 0, 0.7071, 0, 0.7071, 0, 0.7071, 0])
    assert m[:, 1].tolist() == pytest.approx([0, 1, 0.7071, 0, 0, 0.7071, 0, 0.7071])
    assert downmix_matrix(("FL", "FR")).tolist() == [[1, 0], [0, 1]]
    assert downmix_matrix(gains_db={"FC": -6.0})[2, 0] == pytest.approx(0.7071 * 10 ** (-6 / 20))


def _surround_renderer(auto):
    delta = np.zeros((8, 2, 1), dtype=np.float32)
    delta[:, 0, 0] = delta[:, 1, 0] = 0.5                  # every speaker, both ears
    s = Spatializer(delta, speakers=SPEAKERS)
    s.set_levels(0, {})
    s.limit = 1e9
    return Renderer(SPEAKERS, spatializer=s, auto=auto)


def test_spatial_takes_every_speaker():
    r = _surround_renderer(auto=False)
    x = np.zeros((512, 8), dtype=np.float32)
    x[:, 6] = 0.2                                          # SL only
    y = _run(r, x)
    assert y[300].tolist() == pytest.approx([0.1, 0.1])


def test_auto_fades_spatial_in_for_surround_and_out_after_the_hold():
    r = _surround_renderer(auto=True)
    stereo = np.zeros((256 * 4, 8), dtype=np.float32)
    stereo[:, 0] = 0.2
    y = _run(r, stereo)
    assert y[-1].tolist() == pytest.approx([0.2, 0.0])    # the downmix: as it is
    assert not r.surround

    film = stereo.copy()
    film[:, 2] = 0.2                                       # the centre speaks
    y = _run(r, np.concatenate([film] * 10))
    assert r.surround and r.weight == 1.0
    assert y[-1].tolist() == pytest.approx([0.2, 0.2])    # (FL + FC) / 2 to each ear
    # The centre arrives in the downmix one block late (aligned with the
    # spatializer's lookahead); from there to Spatial Audio, a fade.
    assert np.abs(np.diff(y[512:, 1])).max() < 0.005

    r.hold = 3                                             # the hold runs out
    _run(r, np.concatenate([stereo] * 10))
    assert r.weight == 0.0 and not r.surround


def test_the_renderer_carries_its_fade_across_a_reload():
    old = _surround_renderer(auto=True)
    old.hold, old.weight = 50, 1.0
    new = _surround_renderer(auto=True).carry(old)
    assert (new.hold, new.weight) == (50, 1.0)


def test_partitioned_convolution_matches_a_direct_one():
    hrir, _ = load_wav(HRIR_DIR / "ds3d+++.wav")       # 16 800 taps, 66 partitions
    front = front_impulses(hrir)
    s = Spatializer(front)
    s.set_levels(0, {})
    s.limit = 1e9                                          # limiter out of the way
    rng = np.random.default_rng(1)
    x = rng.uniform(-0.1, 0.1, (256 * 80, 2)).astype(np.float32)

    y = _run(s, x)

    left = np.convolve(x[:, 0], front[0, 0]) + np.convolve(x[:, 1], front[1, 0])
    right = np.convolve(x[:, 0], front[0, 1]) + np.convolve(x[:, 1], front[1, 1])
    expected = np.stack([left, right], axis=1)[:len(x) - 256]
    np.testing.assert_allclose(y[256:], expected, atol=2e-4)  # one block of lookahead


def test_immersion_and_speaker_levels_come_before_the_hrir():
    delta = np.zeros((2, 2, 1), dtype=np.float32)
    delta[0, 0, 0] = delta[1, 1, 0] = 1.0                 # FL->L, FR->R, nothing across
    s = Spatializer(delta)
    s.set_levels(50, {"FR": -6.0})                         # +6 dB, and FR at -6 dB
    s.limit = 1e9
    x = np.full((512, 2), 0.1, dtype=np.float32)

    y = _run(s, x)

    assert y[300, 0] == pytest.approx(0.1 * 10 ** (6 / 20), rel=1e-3)
    assert y[300, 1] == pytest.approx(0.1, rel=1e-3)


def test_the_limiter_holds_the_ceiling_without_steps():
    delta = np.zeros((2, 2, 1), dtype=np.float32)
    delta[0, 0, 0] = delta[1, 1, 0] = 1.0
    s = Spatializer(delta)
    s.set_levels(100, {})                                  # +12 dB into a loud tone
    t = np.arange(256 * 40) / 48000
    x = np.stack([0.5 * np.sin(2 * np.pi * 300 * t)] * 2, axis=1).astype(np.float32)

    y = _run(s, x)

    assert np.abs(y).max() <= 10 ** (-1 / 20) + 1e-4
    assert np.abs(np.diff(y[:, 0])).max() < 0.2           # a 300 Hz tone, no click


def test_boost_raises_the_ceiling():
    s = Spatializer(np.zeros((2, 2, 1), dtype=np.float32))
    s.set_boost(6.0)
    assert s.limit == pytest.approx(10 ** (5 / 20))


# ── Microphone ────────────────────────────────────────────────────────────────

def test_a_flat_micro_is_a_plain_copy():
    sos, gain, gate, compressor = build_mic_chain([], 0, 0, 0, 0, {})
    assert sos.shape == (0, 6) and gain == 1.0 and gate is None and compressor is None


def test_micro_stages_map_as_on_linux():
    nr = {"bgReduction": {"enabled": True, "value": 1.0},
          "impactReduction": {"enabled": True, "value": 0.5},
          "noiseGate": {"enabled": True, "value": -80.0},       # clamped to -60
          "compressor": {"enabled": True, "value": 1.0}}

    sos, gain, gate, compressor = build_mic_chain(
        [EqBand(freq=3000, gain=4, q=1)], 0, 2.0, 0, boost_db=6, noise_reduction=nr)

    # Band, the Voice macro, 350 Hz high-pass, 4 kHz shelf at -6 dB.
    assert sos.shape == (4, 6)
    assert gain == pytest.approx(10 ** (6 / 20))                # no headroom on the mic
    db = response_db(sos, 1.0, [100, 350, 16000])
    assert db[0] < -9 and db[1] == pytest.approx(-3.0, abs=0.5)
    assert db[2] == pytest.approx(-6.0, abs=0.5)
    assert gate == {"threshold": -60.0}
    assert compressor["threshold"] == -30.0 and compressor["ratio"] == 8.0
    assert compressor["makeup"] == 10.0


def test_disabled_stages_and_junk_values_are_ignored():
    nr = {"bgReduction": {"enabled": False, "value": 1.0},
          "noiseGate": {"enabled": True, "value": "loud"},
          "compressor": "on"}
    sos, _gain, gate, compressor = build_mic_chain([], 0, 0, 0, 0, nr)
    assert sos.shape == (0, 6) and gate == {"threshold": -40.0} and compressor is None


def test_the_gate_opens_on_the_voice_and_closes_after_the_hold():
    gate = Gate(-40.0)
    voice = np.full((256, 1), 0.1, dtype=np.float32)            # -20 dB: over
    hiss = np.full((256, 1), 0.001, dtype=np.float32)           # -60 dB: under

    for _ in range(2):
        block = voice.copy()
        gate.process(block)
    assert gate.gain == pytest.approx(1.0)

    gains = []
    for _ in range(40):                                         # 213 ms of hiss
        block = hiss.copy()
        gate.process(block)
        gains.append(gate.gain)
    assert gains[7] == pytest.approx(1.0)                       # 50 ms hold
    assert gains[-1] == pytest.approx(gate.floor)               # then 100 ms to shut


def test_the_gate_starts_closed():
    gate = Gate(-40.0)
    block = np.full((256, 1), 0.001, dtype=np.float32)
    gate.process(block)
    assert np.abs(block).max() < 1e-6


@pytest.mark.skipif(not IS_MACOS, reason="vDSP is Accelerate's, macOS only")
def test_a_mono_cascade_filters_one_channel():
    sos, _gain, _gate, _comp = build_mic_chain([EqBand(freq=1000, gain=6, q=1)], 0, 0, 0, 0, {})
    x = np.random.default_rng(2).uniform(-0.5, 0.5, (512, 1)).astype(np.float32)
    y = x.copy()
    StereoBiquads(sos, channels=1).process(y)
    np.testing.assert_allclose(y, _reference(sos, x), atol=2e-4)


# ── Distance (plate reverb) ───────────────────────────────────────────────────

def test_the_plate_is_one_decaying_impulse_per_ear():
    plate = plate_impulses()
    assert plate.shape == (2, 120000)
    assert np.sum(plate[0].astype(np.float64) ** 2) == pytest.approx(1.0, rel=1e-3)
    assert not plate[:, :int(0.008 * 48000)].any()                # pre-delay
    correlation = np.corrcoef(plate[0], plate[1])[0, 1]
    assert abs(correlation) < 0.05                                 # the ears differ


def test_the_plate_loses_its_highs_first():
    plate = plate_impulses()[0]

    def high_share(segment):
        spectrum = np.abs(np.fft.rfft(segment)) ** 2
        freqs = np.fft.rfftfreq(len(segment), 1 / 48000)
        return spectrum[freqs > 6000].sum() / spectrum.sum()

    assert high_share(plate[48000:72000]) < high_share(plate[1000:25000]) / 3


def test_a_diagonal_convolver_keeps_the_channels_apart():
    rng = np.random.default_rng(3)
    impulses = rng.standard_normal((2, 700)).astype(np.float32)
    x = rng.uniform(-0.1, 0.1, (256 * 6, 2)).astype(np.float32)
    convolver = PartitionedConvolver(impulses, 256)

    y = np.concatenate([convolver.process(x[s:s + 256]) for s in range(0, len(x), 256)])

    for ch in range(2):
        np.testing.assert_allclose(y[:, ch], np.convolve(x[:, ch], impulses[ch])[:len(x)],
                                   atol=1e-4)


def _delta_spatializer():
    delta = np.zeros((2, 2, 1), dtype=np.float32)
    delta[0, 0, 0] = delta[1, 1, 0] = 1.0
    s = Spatializer(delta)
    s.set_levels(0, {})
    s.limit = 1e9
    return s


def test_distance_mixes_in_a_tail_that_outlasts_the_sound():
    dry, wet = _delta_spatializer(), _delta_spatializer()
    wet.set_distance(50)
    x = np.zeros((256 * 40, 2), dtype=np.float32)
    x[:256 * 4] = np.random.default_rng(4).uniform(-0.3, 0.3, (256 * 4, 2))

    y_dry, y_wet = _run(dry, x), _run(wet, x)

    assert np.abs(y_dry[256 * 6:]).max() == 0.0                   # dry stops with the sound
    assert np.abs(y_wet[256 * 6:256 * 20]).max() > 1e-3           # the plate rings on
    # Half dry, half plate, while the sound plays.
    np.testing.assert_allclose(y_wet[256:300], y_dry[256:300] * 0.5, atol=1e-6)


def test_distance_at_zero_runs_no_plate():
    s = _delta_spatializer()
    s.set_distance(40)
    s.set_distance(0)
    x = np.full((512, 2), 0.1, dtype=np.float32)
    np.testing.assert_allclose(_run(s, x)[256:], x[:256], atol=1e-6)

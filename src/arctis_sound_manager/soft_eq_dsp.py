# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Sonar EQ chain as plain biquads, for the macOS software equaliser.

On Linux, sonar_to_pipewire turns a channel's EQ into a PipeWire
filter-chain. macOS has no filter-chain, so soft_eq runs the same chain
itself: the same active bands, the same three macro filters and the same
preamp, as second-order sections run by Accelerate's vDSP_biquad; Smart
Volume's compressor; and Spatial Audio's HeSuVi convolution, for the two
front speakers a stereo source has, with Distance's plate reverb. And the
Micro channel's chain, as generate_sonar_micro_conf builds it, but for
noise cancelling.
"""

from __future__ import annotations

import ctypes
import math
import struct
from pathlib import Path

import numpy as np

from arctis_sound_manager.eq_types import EqBand, rbj_coefficients
from arctis_sound_manager.sonar_to_pipewire import (_BAND_FREQ_RANGE, _BAND_GAIN_RANGE,
                                                    _BAND_Q_RANGE, _MACRO_PARAMS,
                                                    _SMART_PRESETS, _clamp_finite,
                                                    _is_active, eq_headroom_db)

SAMPLE_RATE = 48000


def build_chain(bands: list[EqBand], basses_db: float, voix_db: float, aigus_db: float,
                boost_db: float = 0.0, sr: int = SAMPLE_RATE) -> tuple[np.ndarray, float]:
    """(sos, gain) for a channel's EQ: sections of shape (n, 6) and a linear gain.

    A flat channel (no active band, macros and boost at 0) is (no sections,
    gain 1): a plain copy, as _bypass_conf is on Linux. Otherwise the macros
    are always there, and the gain lowers the whole curve by its largest
    boost so no band pushes a full-scale source over it (eq_headroom_db),
    with the user's Boost on top.
    """
    active = [_clamped(b, sr) for b in bands if _is_active(b)]
    macro_values = {"basses": basses_db, "voix": voix_db, "aigus": aigus_db}
    macro_values = {k: _clamp_finite(float(v), *_BAND_GAIN_RANGE, 0.0)
                    for k, v in macro_values.items()}
    boost_db = _clamp_finite(float(boost_db), 0.0, 12.0, 0.0)
    if not active and all(abs(v) < 0.01 for v in macro_values.values()) and abs(boost_db) < 0.01:
        return np.zeros((0, 6)), 1.0

    filters = active + [EqBand(freq=_MACRO_PARAMS[name]["freq"], gain=db,
                               q=_MACRO_PARAMS[name]["q"], type="peakingEQ")
                        for name, db in macro_values.items()]
    sections = []
    for band in filters:
        coefficients = rbj_coefficients(band, sr)
        if coefficients is None:
            continue
        b0, b1, b2, a1, a2 = coefficients
        sections.append([b0, b1, b2, 1.0, a1, a2])
    gain_db = boost_db - eq_headroom_db(active, macro_values)
    return np.array(sections, dtype=np.float64).reshape(-1, 6), 10 ** (gain_db / 20)


def _clamped(band: EqBand, sr: int) -> EqBand:
    """The band with its numbers in range, as _node_block makes them on Linux.

    Also kept below Nyquist, where the cookbook formulas stop making sense.
    """
    top = min(_BAND_FREQ_RANGE[1], sr * 0.45)
    return EqBand(
        freq=_clamp_finite(float(band.freq), _BAND_FREQ_RANGE[0], top, 1000.0),
        gain=_clamp_finite(float(band.gain), *_BAND_GAIN_RANGE, 0.0),
        q=_clamp_finite(float(band.q), *_BAND_Q_RANGE, 0.7071),
        type=band.type,
        enabled=True,
    )


def response_db(sos: np.ndarray, gain: float, freqs, sr: int = SAMPLE_RATE) -> list[float]:
    """The chain's amplitude response in dB at *freqs*, for tests and checks."""
    out = []
    for f in freqs:
        z = complex(math.cos(2 * math.pi * f / sr), math.sin(2 * math.pi * f / sr))
        h = complex(gain)
        for b0, b1, b2, _a0, a1, a2 in sos:
            h *= (b0 + b1 / z + b2 / z ** 2) / (1 + a1 / z + a2 / z ** 2)
        out.append(20 * math.log10(max(abs(h), 1e-12)))
    return out


def smart_volume_params(smart_volume: dict | None) -> dict | None:
    """The compressor Smart Volume asks for, as on Linux (_sc4m_node), or None.

    The mode picks Sonar's loudness target and its timings; the level (0-100)
    takes the ratio from 1 to the mode's and the makeup gain from 0 to the
    mode's. Off, or a level of 0, is no compressor at all.
    """
    if not smart_volume or not smart_volume.get("enabled"):
        return None
    preset = _SMART_PRESETS.get(smart_volume.get("loudness"), _SMART_PRESETS["balanced"])
    level = _clamp_finite(float(smart_volume.get("level", 50)), 0.0, 100.0, 50.0) / 100.0
    if level <= 0.0:
        return None
    return {**preset,
            "ratio": 1.0 + (preset["ratio"] - 1.0) * level,
            "makeup": preset["makeup"] * level}


def build_mic_chain(bands: list[EqBand], basses_db: float, voix_db: float, aigus_db: float,
                    boost_db: float, noise_reduction: dict | None,
                    sr: int = SAMPLE_RATE) -> tuple[np.ndarray, float, dict | None, dict | None]:
    """(sos, gain, gate, compressor) for the Micro channel.

    The same stages, in the same order and with the same mappings, as
    generate_sonar_micro_conf: the bands and macros, Boost as a plain gain
    (the microphone gets no headroom stage), Background Noise Reduction as a
    30-350 Hz high-pass, Impact Noise Reduction as a 4 kHz shelf down to
    -12 dB, then the noise gate and the compressor, as parameters for Gate
    and Compressor (None when off).
    """
    nr = noise_reduction or {}

    def stage(key: str) -> dict:
        value = nr.get(key)
        return value if isinstance(value, dict) and value.get("enabled") else {}

    def amount(cfg: dict, default: float, lo: float = 0.0, hi: float = 1.0) -> float:
        try:
            return _clamp_finite(float(cfg.get("value", default)), lo, hi, default)
        except (TypeError, ValueError):
            return default

    filters = [_clamped(b, sr) for b in bands if _is_active(b)]
    for name, db in (("basses", basses_db), ("voix", voix_db), ("aigus", aigus_db)):
        db = _clamp_finite(float(db), *_BAND_GAIN_RANGE, 0.0)
        if abs(db) >= 0.01:
            filters.append(EqBand(freq=_MACRO_PARAMS[name]["freq"], gain=db,
                                  q=_MACRO_PARAMS[name]["q"], type="peakingEQ"))
    if bg := stage("bgReduction"):
        filters.append(EqBand(freq=30.0 + amount(bg, 0.0) * 320.0, q=0.7071, type="highPass"))
    if impact := stage("impactReduction"):
        cut = -amount(impact, 0.0) * 12.0
        if abs(cut) >= 0.01:
            filters.append(EqBand(freq=4000.0, gain=cut, q=0.7071, type="highShelving"))
    sections = []
    for band in filters:
        b0, b1, b2, a1, a2 = rbj_coefficients(band, sr)
        sections.append([b0, b1, b2, 1.0, a1, a2])
    sos = np.array(sections, dtype=np.float64).reshape(-1, 6)
    gain = 10 ** (_clamp_finite(float(boost_db), -12.0, 12.0, 0.0) / 20)

    gate = None
    if ng := stage("noiseGate"):
        gate = {"threshold": amount(ng, -40.0, -60.0, -10.0)}
    compressor = None
    if comp := stage("compressor"):
        v = amount(comp, 0.0)
        compressor = {"threshold": -10.0 - v * 20.0, "ratio": 2.0 + v * 6.0,
                      "makeup": v * 10.0, "knee": 6.0, "attack": 10.0, "release": 150.0}
    return sos, gain, gate, compressor


class Gate:
    """The micro's noise gate, as SWH's gate in the Linux chain.

    Open while the level is over the threshold and for 50 ms after, opening
    in 5 ms and closing over 100 ms, down to -90 dB. Followed once per block,
    the gain ramped across it; in place, on frames of shape (n, channels).
    """

    def __init__(self, threshold_db: float, sr: int = SAMPLE_RATE, attack_ms: float = 5.0,
                 hold_ms: float = 50.0, decay_ms: float = 100.0, range_db: float = -90.0):
        self.threshold = 10 ** (threshold_db / 20)
        self.sr = sr
        self.attack_s, self.hold_s, self.decay_s = attack_ms / 1000, hold_ms / 1000, decay_ms / 1000
        self.floor = 10 ** (range_db / 20)
        self.gain = self.floor
        self._hold_left = 0.0

    def process(self, frames: np.ndarray) -> None:
        n = frames.shape[0]
        if not n:
            return
        seconds = n / self.sr
        if float(np.max(np.abs(frames))) >= self.threshold:
            self._hold_left = self.hold_s
        else:
            self._hold_left = max(0.0, self._hold_left - seconds)
        if self._hold_left > 0:
            target = min(1.0, self.gain + seconds / self.attack_s)
        else:
            target = max(self.floor, self.gain - seconds / self.decay_s)
        ramp = np.linspace(self.gain, target, n, endpoint=False, dtype=np.float32)
        frames *= ramp[:, None]
        self.gain = target


class Compressor:
    """Smart Volume: an RMS compressor with a soft knee, like SWH's sc4m.

    The channels share one detector, so the stereo image holds. The level is
    followed once per block (5 ms at 256 frames, the scale of the shortest
    attack), and the gain ramps across each block to the new value, so it
    never steps. In place, on float32 frames of shape (n, channels).
    """

    def __init__(self, params: dict, sr: int = SAMPLE_RATE):
        self.threshold = float(params["threshold"])
        self.ratio = max(1.0, float(params["ratio"]))
        self.knee = max(0.0, float(params["knee"]))
        self.makeup = float(params["makeup"])
        self.attack_s = max(1e-4, float(params["attack"]) / 1000)
        self.release_s = max(1e-4, float(params["release"]) / 1000)
        self.sr = sr
        self.envelope = 0.0          # RMS, linear
        self.gain = 10 ** (self.makeup / 20)

    def gain_db(self, level_db: float) -> float:
        """Gain for a detected level: the knee's curve, then the makeup."""
        over = level_db - self.threshold
        slope = 1 / self.ratio - 1
        if 2 * over < -self.knee:
            reduction = 0.0
        elif self.knee and 2 * abs(over) <= self.knee:
            reduction = slope * (over + self.knee / 2) ** 2 / (2 * self.knee)
        else:
            reduction = slope * over
        return reduction + self.makeup

    def process(self, frames: np.ndarray) -> None:
        n = frames.shape[0]
        if not n:
            return
        # The level of the loudest pair: a stereo source on a 7.1 route,
        # its other speakers silent, reads as loud as on a stereo one.
        pairs = min(frames.shape[1], 2) if frames.ndim > 1 else 1
        rms = math.sqrt(float(np.sum(np.square(frames, dtype=np.float32))) / (n * pairs))
        seconds = n / self.sr
        tau = self.attack_s if rms > self.envelope else self.release_s
        self.envelope = rms + math.exp(-seconds / tau) * (self.envelope - rms)
        level_db = 20 * math.log10(max(self.envelope, 1e-9))
        target = 10 ** (self.gain_db(level_db) / 20)
        ramp = np.linspace(self.gain, target, n, endpoint=False, dtype=np.float32) \
            if target != self.gain else np.float32(target)
        frames *= ramp[:, None] if np.ndim(ramp) else ramp
        self.gain = target


class StereoBiquads:
    """A cascade of biquads over interleaved frames, with state across blocks.

    Runs on Accelerate's vDSP_biquad (macOS): one setup for the chain, one
    delay line per channel, each channel read and written in place at a
    stride of the channel count. Build a new one when the chain changes;
    its delay lines start from silence.
    """

    def __init__(self, sos: np.ndarray, channels: int = 2):
        self.sections = len(sos)
        self.channels = channels
        self._setup = None
        if self.sections:
            vdsp = _vdsp()
            coefficients = np.ascontiguousarray(
                np.asarray(sos, dtype=np.float64)[:, [0, 1, 2, 4, 5]].reshape(-1))
            self._setup = vdsp.vDSP_biquad_CreateSetup(
                coefficients.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), self.sections)
            if not self._setup:
                raise MemoryError("vDSP_biquad_CreateSetup failed")
        self._delays = [np.zeros(2 * self.sections + 2, dtype=np.float32)
                        for _ in range(channels)]

    def process(self, frames: np.ndarray) -> None:
        """Filter *frames* (float32, C-contiguous, shape (n, channels)) in place."""
        if not self._setup:
            return
        vdsp = _vdsp()
        n = frames.shape[0]
        base = frames.ctypes.data
        for ch, delay in enumerate(self._delays):
            pointer = ctypes.cast(base + 4 * ch, ctypes.POINTER(ctypes.c_float))
            vdsp.vDSP_biquad(self._setup, delay.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                             pointer, self.channels, pointer, self.channels, n)

    def close(self) -> None:
        if self._setup:
            _vdsp().vDSP_biquad_DestroySetup(self._setup)
            self._setup = None

    def __del__(self):
        self.close()


_VDSP = None


def _vdsp():
    global _VDSP
    if _VDSP is None:
        # By path: find_library searches differently inside a frozen app.
        lib = ctypes.CDLL("/System/Library/Frameworks/Accelerate.framework/Accelerate")
        lib.vDSP_biquad_CreateSetup.restype = ctypes.c_void_p
        lib.vDSP_biquad_CreateSetup.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_ulong]
        lib.vDSP_biquad_DestroySetup.restype = None
        lib.vDSP_biquad_DestroySetup.argtypes = [ctypes.c_void_p]
        lib.vDSP_biquad.restype = None
        lib.vDSP_biquad.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float),
                                    ctypes.POINTER(ctypes.c_float), ctypes.c_long,
                                    ctypes.POINTER(ctypes.c_float), ctypes.c_long, ctypes.c_ulong]
        _VDSP = lib
    return _VDSP


# ── Spatial Audio ─────────────────────────────────────────────────────────────

#: The source speakers, in the order Linux's 7.1 chains carry them.
SPEAKERS = ("FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR")
STEREO = ("FL", "FR")

#: HeSuVi's 14-channel layout, as sonar_to_pipewire._HESUVI_CONVOLVERS reads
#: it: (speaker, ear) -> the WAV channel with that speaker's impulse to that
#: ear (0 left, 1 right). LFE has no impulse of its own: it takes FC's.
_HESUVI_14 = {("FL", 0): 0, ("FL", 1): 1, ("SL", 0): 2, ("SL", 1): 3, ("RL", 0): 4,
              ("RL", 1): 5, ("FC", 0): 6, ("FR", 1): 7, ("FR", 0): 8, ("SR", 1): 9,
              ("SR", 0): 10, ("RR", 1): 11, ("RR", 0): 12, ("FC", 1): 13}
#: The 7-channel files are the left half only; the right side mirrors it.
_HESUVI_7 = {("FL", 0): 0, ("FL", 1): 1, ("SL", 0): 2, ("SL", 1): 3, ("RL", 0): 4,
             ("RL", 1): 5, ("FC", 0): 6, ("FC", 1): 6, ("FR", 1): 0, ("FR", 0): 1,
             ("SR", 1): 2, ("SR", 0): 3, ("RR", 1): 4, ("RR", 0): 5}
#: ITU-R BS.775 stereo downmix, as Linux's: centre and surrounds at -3 dB
#: into their side, LFE left out.
_DOWNMIX = ({"FL": 1.0, "FC": 0.7071, "SL": 0.7071, "RL": 0.7071},
            {"FR": 1.0, "FC": 0.7071, "SR": 0.7071, "RR": 0.7071})
_LIMIT_DB = -1.0


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    """(frames, channels) float32 samples and the rate of a WAV file.

    HeSuVi HRIRs are 32-bit float, which the wave module does not read;
    integer PCM is read too, for a WAV the user supplies.
    """
    data = Path(path).read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"{path}: not a WAV file")
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8:pos + 8 + size]
        if chunk == b"fmt ":
            tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", body)
            if tag == 0xFFFE and len(body) >= 26:          # WAVE_FORMAT_EXTENSIBLE
                tag = struct.unpack_from("<H", body, 24)[0]
            fmt = tag, channels, rate, bits
        elif chunk == b"data" and fmt is not None:
            tag, channels, rate, bits = fmt
            if tag == 3 and bits in (32, 64):
                samples = np.frombuffer(body, dtype="<f4" if bits == 32 else "<f8")
            elif tag == 1 and bits in (16, 32):
                samples = np.frombuffer(body, dtype=f"<i{bits // 8}") / float(2 ** (bits - 1))
            elif tag == 1 and bits == 24:
                raw = np.frombuffer(body[:len(body) // 3 * 3], dtype=np.uint8).reshape(-1, 3)
                ints = (raw[:, 0].astype(np.int32) | raw[:, 1].astype(np.int32) << 8
                        | raw[:, 2].astype(np.int32) << 16)
                samples = np.where(ints >= 1 << 23, ints - (1 << 24), ints) / float(1 << 23)
            else:
                raise ValueError(f"{path}: unsupported WAV format {tag}/{bits} bits")
            frames = len(samples) // channels
            return samples[:frames * channels].reshape(frames, channels).astype(np.float32), rate
        pos += 8 + size + (size & 1)
    raise ValueError(f"{path}: no audio data")


def hesuvi_impulses(hrir: np.ndarray, speakers=SPEAKERS) -> np.ndarray:
    """(speakers, 2 ears, taps): each speaker's impulse to the left and right ear."""
    if hrir.shape[1] < 7:
        raise ValueError(f"a HeSuVi HRIR has 7 or 14 channels, not {hrir.shape[1]}")
    layout = _HESUVI_14 if hrir.shape[1] >= 14 else _HESUVI_7
    out = np.zeros((len(speakers), 2, hrir.shape[0]), dtype=np.float32)
    for i, speaker in enumerate(speakers):
        for ear in (0, 1):
            out[i, ear] = hrir[:, layout["FC" if speaker == "LFE" else speaker, ear]]
    return out


def front_impulses(hrir: np.ndarray) -> np.ndarray:
    """(2 speakers, 2 ears, taps): FL and FR to the left and right ear."""
    return hesuvi_impulses(hrir, STEREO)


def downmix_matrix(speakers=SPEAKERS, gains_db: dict | None = None) -> np.ndarray:
    """(speakers, 2): the stereo fold of *speakers*, as Linux's downmix.

    A stereo source comes out as it is. *gains_db*, Speakers mode's levels,
    scale each speaker's share.
    """
    gains_db = gains_db or {}
    out = np.zeros((len(speakers), 2), dtype=np.float32)
    for i, speaker in enumerate(speakers):
        level = 10 ** (float(gains_db.get(speaker, 0.0)) / 20)
        for side in (0, 1):
            out[i, side] = _DOWNMIX[side].get(speaker, 0.0) * level
    return out


class PartitionedConvolver:
    """Uniformly partitioned convolution, one block at a time (overlap-save).

    *impulses* is (inputs, outputs, taps): every input convolved with its
    impulse to every output, summed per output. Or (channels, taps): each
    channel convolved with its own impulse only, at half the cost of the
    same thing with zeros across. Each block costs one FFT per input and
    output and a multiply-add over the partitions, whatever the length of
    the impulses.
    """

    def __init__(self, impulses: np.ndarray, block: int):
        self.block = block
        self.diagonal = impulses.ndim == 2
        inputs, taps = impulses.shape[0], impulses.shape[-1]
        self.partitions = max(1, -(-taps // block))
        padded = np.zeros(impulses.shape[:-1] + (self.partitions * block,), dtype=np.float32)
        padded[..., :taps] = impulses
        parts = padded.reshape(impulses.shape[:-1] + (self.partitions, block))
        # Each partition zero-padded to 2*block: overlap-save, last half kept.
        self._filters = np.fft.rfft(parts, n=2 * block, axis=-1).astype(np.complex64)
        # The input spectra, newest first, written twice so the last
        # `partitions` of them are always one contiguous slice.
        self._history = np.zeros((inputs, 2 * self.partitions, block + 1), dtype=np.complex64)
        self._head = 0
        self._previous = np.zeros((block, inputs), dtype=np.float32)

    def process(self, frames: np.ndarray) -> np.ndarray:
        """(block, inputs) in, (block, outputs) out."""
        window = np.concatenate((self._previous, frames))             # (2*block, inputs)
        self._previous = np.array(frames, dtype=np.float32)
        self._head = (self._head - 1) % self.partitions
        spectrum = np.fft.rfft(window, axis=0).T
        self._history[:, self._head] = spectrum
        self._history[:, self._head + self.partitions] = spectrum
        recent = self._history[:, self._head:self._head + self.partitions]
        if self.diagonal:
            out = (recent * self._filters).sum(axis=1)
        else:
            out = np.einsum("ipk,iopk->ok", recent, self._filters)
        return np.fft.irfft(out, n=2 * self.block, axis=-1)[:, self.block:].T


def plate_impulses(seconds: float = 2.5, damping: float = 0.5, sr: int = SAMPLE_RATE,
                   seed: int = 1423) -> np.ndarray:
    """A plate reverb's impulse for each ear, (2, taps), as a convolver takes it.

    Linux runs SWH's plate (reverb time 2.5, damping 0.5) on each ear. Here
    each ear gets its own decaying noise, so the two stay decorrelated, and
    nothing crosses between them. Higher bands die out sooner, the more so
    the more damping, and the onset fades in after a short pre-delay, as a
    plate's does. Each impulse has unit energy: as loud as the dry sound.
    """
    taps = int(seconds * sr)
    t = np.arange(taps) / sr
    freqs = np.fft.rfftfreq(taps, 1 / sr)
    rng = np.random.default_rng(seed)
    # (upper edge Hz, how much of the reverb time the band lasts at damping 1)
    bands = ((500, 1.0), (2000, 0.7), (6000, 0.4), (sr / 2 + 1, 0.15))
    out = np.zeros((2, taps), dtype=np.float32)
    for ear in range(2):
        spectrum = np.fft.rfft(rng.standard_normal(taps))
        ir = np.zeros(taps)
        low = 0.0
        for high, share in bands:
            band = np.fft.irfft(np.where((freqs >= low) & (freqs < high), spectrum, 0), n=taps)
            rt60 = seconds * (1 - damping * (1 - share))
            ir += band * np.exp(-6.91 * t / rt60)
            low = high
        onset = np.clip((t - 0.008) / 0.012, 0.0, 1.0)              # 8 ms, then 12 ms in
        ir *= onset
        out[ear] = ir / np.sqrt(np.sum(ir ** 2))
    return out


class Spatializer:
    """Spatial Audio: the source's speakers as virtual ones, binaurally.

    Each speaker is convolved with its HRIR to each ear, as the HeSuVi
    chain does on Linux (Immersion and the speaker's level before it,
    Distance's plate reverb and a -1 dBFS limiter after it). The convolution is uniformly partitioned in
    blocks of *block* frames, so a 16 800-tap HRIR costs a few FFTs a block;
    process() takes exactly one block at a time. The limiter looks one block
    ahead, so its gain glides into a peak instead of stepping: one block of
    latency (5 ms at 256 frames). *impulses* is (speakers, 2, taps), for the
    *speakers* the source's channels are, in that order.
    """

    def __init__(self, impulses: np.ndarray, block: int = 256, sr: int = SAMPLE_RATE,
                 speakers=STEREO):
        if len(speakers) != impulses.shape[0]:
            raise ValueError(f"{impulses.shape[0]} impulses for {len(speakers)} speakers")
        self.speakers = tuple(speakers)
        self.block = block
        self.sr = sr
        self._hrir = PartitionedConvolver(impulses, block)
        self.partitions = self._hrir.partitions
        self._plate: PartitionedConvolver | None = None
        self.wet = 0.0
        self.input_gains = np.ones(len(self.speakers), dtype=np.float32)
        self.limit = 10 ** (_LIMIT_DB / 20)
        self._limiter = 1.0
        self._release = math.exp(-block / (0.1 * sr))       # 0.1 s, as on Linux
        self._pending = np.zeros((block, 2), dtype=np.float32)
        self._pending_needed = 1.0

    def set_levels(self, immersion_pct: float, gains_db: dict | None) -> None:
        """Immersion (0-100 %: 0 to +12 dB) plus each speaker's level."""
        immersion_db = max(0.0, min(100.0, float(immersion_pct))) / 100 * 12
        gains_db = gains_db or {}
        self.input_gains = np.array(
            [10 ** ((immersion_db + float(gains_db.get(ch, 0.0))) / 20) for ch in self.speakers],
            dtype=np.float32)

    def set_distance(self, distance_pct: float) -> None:
        """Distance (0-100 %): the plate reverb's dry/wet mix, as on Linux.

        The plate is built the first time it is wanted and kept; at 0 it is
        not run at all, and it starts from silence when it comes back.
        """
        wet = max(0.0, min(100.0, float(distance_pct))) / 100
        if wet > 0 and (self._plate is None or self.wet == 0):
            self._plate = PartitionedConvolver(plate_impulses(sr=self.sr), self.block)
        self.wet = wet

    def set_boost(self, boost_db: float) -> None:
        """The limiter lets Boost through, as the Linux chain re-applies it."""
        self.limit = 10 ** ((_LIMIT_DB + max(0.0, boost_db)) / 20)

    def process(self, frames: np.ndarray) -> np.ndarray:
        """(block, speakers) in, the (block, 2) ears out, one block late."""
        if frames.shape[0] != self.block:
            raise ValueError(f"Spatializer takes blocks of {self.block} frames")
        out = self._hrir.process(frames * self.input_gains)
        if self.wet > 0 and self._plate is not None:
            out = out * (1 - self.wet) + self._plate.process(out) * self.wet
        # Peak limiter. The gain that comes out of the block played now must
        # suit it and the block after it, and it never rises faster than the
        # 0.1 s release: ramping to that, the whole block stays under the
        # ceiling and the gain never steps.
        peak = float(np.max(np.abs(out)))
        needed = min(1.0, self.limit / peak) if peak > 0 else 1.0
        recovered = 1.0 - (1.0 - self._limiter) * self._release
        end = min(self._pending_needed, needed, recovered)
        ramp = np.linspace(self._limiter, end, self.block, endpoint=False, dtype=np.float32)
        played = self._pending * ramp[:, None]
        self._limiter = end
        self._pending = out.astype(np.float32)
        self._pending_needed = needed
        return played


class Renderer:
    """From the source's speakers to the headset's two channels.

    Spatial Audio when there is a *spatializer*, else the stereo downmix
    (Spatial off, or Speakers mode with its levels in *downmix*). With
    *auto*, Spatial Audio only for surround sources, as Linux's "surround
    sources only": macOS does not say what an app plays, so a source is
    surround while anything comes out of the speakers beyond FL/FR, and
    for a while after (a film's surrounds go quiet between scenes). It
    crossfades between the two over a tenth of a second; both run all the
    time, so the convolution has the sound's past when it fades in, and the
    downmix is delayed by the spatializer's block of lookahead to line up.
    """

    HOLD_S = 10.0
    FADE_S = 0.1
    SILENCE = 1e-5

    def __init__(self, speakers=STEREO, downmix: np.ndarray | None = None,
                 spatializer: Spatializer | None = None, auto: bool = False,
                 block: int = 256, sr: int = SAMPLE_RATE):
        self.speakers = tuple(speakers)
        self.downmix = downmix_matrix(self.speakers) if downmix is None else downmix
        self.spatializer = spatializer
        self.auto = auto and spatializer is not None
        self.block = block
        self._surround = [i for i, s in enumerate(self.speakers) if s not in STEREO]
        self._hold_blocks = int(self.HOLD_S * sr / block)
        self._step = block / (self.FADE_S * sr)
        self.hold = 0               # blocks of surround left to wait out
        self.weight = 0.0           # 0 downmix, 1 Spatial Audio
        self._dry = np.zeros((block, 2), dtype=np.float32)

    @property
    def inputs(self) -> int:
        return len(self.speakers)

    def carry(self, previous: "Renderer | None") -> "Renderer":
        """Keep where the old one was in its fade and hold."""
        if previous is not None and previous.auto and self.auto:
            self.hold, self.weight = previous.hold, previous.weight
        return self

    @property
    def surround(self) -> bool:
        """Whether a surround source plays (auto) or Spatial Audio is on at all."""
        return self.spatializer is not None and (not self.auto or self.weight > 0)

    def process(self, frames: np.ndarray) -> np.ndarray:
        if self.spatializer is None or frames.shape[0] != self.block:
            return frames @ self.downmix
        wet = self.spatializer.process(frames)
        if not self.auto:
            return wet
        if self._surround and float(np.max(np.abs(frames[:, self._surround]))) > self.SILENCE:
            self.hold = self._hold_blocks
        elif self.hold:
            self.hold -= 1
        target = 1.0 if self.hold else 0.0
        start = self.weight
        self.weight = min(target, start + self._step) if target > start \
            else max(target, start - self._step)
        dry, self._dry = self._dry, frames @ self.downmix
        if start == self.weight == 0.0:
            return dry
        if start == self.weight == 1.0:
            return wet
        ramp = np.linspace(start, self.weight, self.block, endpoint=False,
                           dtype=np.float32)[:, None]
        return dry + (wet - dry) * ramp

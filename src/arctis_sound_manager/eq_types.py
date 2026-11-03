# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""
eq_types.py — Shared EQ data types used by both the daemon and the GUI.

Kept free of any Qt / PySide6 imports so the daemon-side modules
(sonar_to_pipewire, dbus_service) can import EqBand and PW_LABEL without
pulling in the entire Qt stack.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ── PipeWire biquad filter label map ─────────────────────────────────────────

PW_LABEL: dict[str, str] = {
    "peakingEQ":    "bq_peaking",
    "lowPass":      "bq_lowpass",
    "highPass":     "bq_highpass",
    "lowShelving":  "bq_lowshelf",
    "highShelving": "bq_highshelf",
    # Sonar's other filter types (SoundStage.Models Filter.FilterType, the
    # RBJ cookbook set). PipeWire's bandpass is the constant 0 dB peak one;
    # bandPassPeakQ (peak gain = Q) is that plus a linear gain stage, see
    # sonar_to_pipewire._band_slot_rack. byPass is never emitted.
    "notchFilter":     "bq_notch",
    "allPass":         "bq_allpass",
    "bandPassPeak0dB": "bq_bandpass",
    "bandPassPeakQ":   "bq_bandpass",
}

# ── EQ band dataclass ─────────────────────────────────────────────────────────


@dataclass
class EqBand:
    freq: float = 1000.0
    gain: float = 0.0
    q: float = 0.707
    type: str = "peakingEQ"
    enabled: bool = True


# ── Biquad coefficients ───────────────────────────────────────────────────────


def rbj_coefficients(band: EqBand, sr: float) -> tuple[float, float, float, float, float] | None:
    """Normalised biquad coefficients (b0, b1, b2, a1, a2) for *band* at *sr*.

    The RBJ Audio EQ Cookbook formulas for Sonar's filter types. PipeWire
    computes its own from the bq_* labels; the curve widget draws from
    these, and the macOS software equaliser (soft_eq_dsp) runs them.
    bandPassPeakQ is the constant skirt gain bandpass, whose peak is Q:
    what PipeWire builds as a bandpass followed by a gain of Q. None for a
    type with no filter (byPass, or anything unknown).
    """
    f0, gain_db, q = band.freq, band.gain, band.q
    a = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / sr
    cosw = math.cos(w0)
    sinw = math.sin(w0)
    alpha = sinw / (2 * q)

    t = band.type
    if t == "peakingEQ":
        b0 = 1 + alpha * a;  b1 = -2 * cosw;  b2 = 1 - alpha * a
        a0 = 1 + alpha / a;  a1 = -2 * cosw;  a2 = 1 - alpha / a
    elif t == "lowPass":
        b0 = (1 - cosw) / 2; b1 = 1 - cosw;   b2 = (1 - cosw) / 2
        a0 = 1 + alpha;      a1 = -2 * cosw;  a2 = 1 - alpha
    elif t == "highPass":
        b0 = (1 + cosw) / 2; b1 = -(1 + cosw); b2 = (1 + cosw) / 2
        a0 = 1 + alpha;      a1 = -2 * cosw;   a2 = 1 - alpha
    elif t == "lowShelving":
        sa = math.sqrt(a)
        b0 =      a * ((a + 1) - (a - 1) * cosw + 2 * sa * alpha)
        b1 =  2 * a * ((a - 1) - (a + 1) * cosw)
        b2 =      a * ((a + 1) - (a - 1) * cosw - 2 * sa * alpha)
        a0 =           (a + 1) + (a - 1) * cosw + 2 * sa * alpha
        a1 = -2 *     ((a - 1) + (a + 1) * cosw)
        a2 =           (a + 1) + (a - 1) * cosw - 2 * sa * alpha
    elif t == "highShelving":
        sa = math.sqrt(a)
        b0 =      a * ((a + 1) + (a - 1) * cosw + 2 * sa * alpha)
        b1 = -2 * a * ((a - 1) + (a + 1) * cosw)
        b2 =      a * ((a + 1) + (a - 1) * cosw - 2 * sa * alpha)
        a0 =           (a + 1) - (a - 1) * cosw + 2 * sa * alpha
        a1 =  2 *     ((a - 1) - (a + 1) * cosw)
        a2 =           (a + 1) - (a - 1) * cosw - 2 * sa * alpha
    elif t == "bandPassPeak0dB":
        b0 = alpha;          b1 = 0.0;        b2 = -alpha
        a0 = 1 + alpha;      a1 = -2 * cosw;  a2 = 1 - alpha
    elif t == "bandPassPeakQ":
        b0 = sinw / 2;       b1 = 0.0;        b2 = -sinw / 2
        a0 = 1 + alpha;      a1 = -2 * cosw;  a2 = 1 - alpha
    elif t == "notchFilter":
        b0 = 1.0;            b1 = -2 * cosw;  b2 = 1.0
        a0 = 1 + alpha;      a1 = -2 * cosw;  a2 = 1 - alpha
    elif t == "allPass":
        b0 = 1 - alpha;      b1 = -2 * cosw;  b2 = 1 + alpha
        a0 = 1 + alpha;      a1 = -2 * cosw;  a2 = 1 - alpha
    else:
        return None
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0

# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""An Arctis 7 dongle failed every status read with EOVERFLOW (errno 75),
twice a second for hours, and the headset stayed silent until ASM was quit
(#317). The listen loop logged each one and did nothing else. A persistent
overflow now gets the wedged-device USB reset, once, and is logged sparingly.
"""

import asyncio
import errno
import threading
from collections import deque
from unittest.mock import MagicMock

import usb.core

from arctis_sound_manager import core as core_mod
from arctis_sound_manager.core import CoreEngine


def _overflow(*a, **k):
    raise usb.core.USBError("Overflow", errno=errno.EOVERFLOW)


def _engine(reads):
    engine = MagicMock()
    engine._device_lock = threading.RLock()
    engine._untagged_requests_pending = deque()
    engine._eio_counts = {}
    engine._overflow_counts = {}
    engine._overflow_reset_spent = False
    engine._enodev_count = 0
    engine.device_config.status = None
    engine.guess_interface_endpoint.return_value = (0x83, 32)
    engine.usb_device.read.side_effect = reads
    engine._on_listen_overflow = (
        lambda i, e: CoreEngine._on_listen_overflow(engine, i, e))
    return engine


def _run(engine, monkeypatch, iterations):
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *a, **k):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(core_mod.asyncio, "sleep", fake_sleep)

    async def go():
        for _ in range(iterations):
            await CoreEngine.listen_endpoint_loop(engine, 5)

    asyncio.run(go())
    return sleeps


def test_a_persistent_overflow_resets_the_device_once(monkeypatch):
    engine = _engine(_overflow)

    sleeps = _run(engine, monkeypatch, 3 * core_mod._OVERFLOW_RESET_STREAK)

    engine._schedule_wedged_device_reset.assert_called_once()
    assert min(sleeps) >= 0.5


def test_a_short_streak_does_not_reset(monkeypatch):
    engine = _engine(_overflow)

    _run(engine, monkeypatch, core_mod._OVERFLOW_RESET_STREAK - 1)

    engine._schedule_wedged_device_reset.assert_not_called()


def test_a_good_read_ends_the_streak(monkeypatch):
    n = core_mod._OVERFLOW_RESET_STREAK - 1
    outcomes = [usb.core.USBError("Overflow", errno=errno.EOVERFLOW)] * n + [[0] * 8] \
        + [usb.core.USBError("Overflow", errno=errno.EOVERFLOW)] * n
    engine = _engine(outcomes)

    _run(engine, monkeypatch, len(outcomes))

    engine._schedule_wedged_device_reset.assert_not_called()


def test_the_flood_is_not_logged_read_by_read(monkeypatch):
    engine = _engine(_overflow)

    _run(engine, monkeypatch, 99)

    assert engine.logger.warning.call_count == 1

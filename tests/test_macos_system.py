# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""macOS sleep/wake and presence, in place of logind."""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading

import pytest

from arctis_sound_manager import macos_system as ms


class FakeIOKit:
    def __init__(self):
        self.allowed: list[tuple[int, object]] = []

    def IOAllowPowerChange(self, port, argument):
        self.allowed.append((port, argument))
        return 0


def _watcher(loop, on_sleep=lambda: None, on_wake=None):
    calls = []

    async def wake():
        calls.append('wake')

    w = ms.SystemPowerWatcher(loop, on_sleep, on_wake or wake, libs=(FakeIOKit(), None))
    w._root_port = 42
    return w, calls


def _from_thread(w, message, argument):
    """IOKit calls back on the run loop thread, never the daemon's loop."""
    t = threading.Thread(target=w.handle, args=(message, argument))
    t.start()
    return t


async def _join(t):
    while t.is_alive():
        await asyncio.sleep(0.01)


def test_sleep_releases_the_headset_on_the_loop_then_allows_it():
    async def main():
        loop = asyncio.get_running_loop()
        seen = []
        w, _ = _watcher(loop, on_sleep=lambda: seen.append(threading.current_thread()))
        await _join(_from_thread(w, ms.MSG_SYSTEM_WILL_SLEEP, 7))
        assert seen == [threading.main_thread()], "prepare_for_sleep runs on the daemon's loop"
        assert w._libs[0].allowed == [(42, 7)]
    asyncio.run(main())


def test_a_failing_prepare_still_lets_the_mac_sleep():
    async def main():
        def boom():
            raise RuntimeError('usb')
        w, _ = _watcher(asyncio.get_running_loop(), on_sleep=boom)
        await _join(_from_thread(w, ms.MSG_SYSTEM_WILL_SLEEP, 3))
        assert w._libs[0].allowed == [(42, 3)]
    asyncio.run(main())


def test_a_hung_prepare_does_not_hold_the_sleep_forever(monkeypatch):
    monkeypatch.setattr(ms, '_SLEEP_PREPARE_TIMEOUT_S', 0.05)

    async def main():
        w, _ = _watcher(asyncio.get_running_loop(), on_sleep=lambda: None)
        # The loop is busy: the prepare call never gets to run in time.
        t = _from_thread(w, ms.MSG_SYSTEM_WILL_SLEEP, 5)
        t.join(1.0)
        assert w._libs[0].allowed == [(42, 5)]
    asyncio.run(main())


def test_idle_sleep_is_allowed_without_releasing_anything():
    async def main():
        seen = []
        w, _ = _watcher(asyncio.get_running_loop(), on_sleep=lambda: seen.append(1))
        await _join(_from_thread(w, ms.MSG_CAN_SYSTEM_SLEEP, 9))
        assert seen == []
        assert w._libs[0].allowed == [(42, 9)]
    asyncio.run(main())


def test_wake_re_acquires_the_headset():
    async def main():
        w, calls = _watcher(asyncio.get_running_loop())
        await _join(_from_thread(w, ms.MSG_SYSTEM_HAS_POWERED_ON, None))
        await asyncio.sleep(0.05)
        assert calls == ['wake']
        assert w._libs[0].allowed == []
    asyncio.run(main())


def test_a_failing_wake_is_logged_not_raised(caplog):
    async def main():
        async def boom():
            raise RuntimeError('gone')
        w, _ = _watcher(asyncio.get_running_loop(), on_wake=boom)
        await _join(_from_thread(w, ms.MSG_SYSTEM_HAS_POWERED_ON, None))
        await asyncio.sleep(0.05)
    asyncio.run(main())
    assert 'resume_from_sleep failed' in caplog.text


def test_an_exception_never_crosses_into_iokit(caplog, monkeypatch):
    w, _ = _watcher(None)

    def boom(message, argument):
        raise RuntimeError('bug')
    monkeypatch.setattr(w, 'handle', boom)
    w._on_message(None, 0, ms.MSG_SYSTEM_HAS_POWERED_ON, None)
    assert 'not handled' in caplog.text


def test_the_daemon_watches_sleep_on_macos():
    from arctis_sound_manager.scripts import daemon

    src = inspect.getsource(daemon.main_async)
    assert 'SystemPowerWatcher' in src
    assert 'core_engine.prepare_for_sleep' in src and 'core_engine.resume_from_sleep' in src


@pytest.mark.skipif(sys.platform != 'darwin', reason='IOKit is macOS only')
def test_registers_with_iokit_and_stops():
    async def main():
        w = ms.SystemPowerWatcher(asyncio.get_running_loop(), lambda: None,
                                  lambda: asyncio.sleep(0))
        assert w.start()
        w.stop()
        assert not w._thread.is_alive()
        assert w._root_port == 0
    asyncio.run(main())


# ── presence ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('locked, asleep, idle, away', [
    (False, False, 10.0, False),
    (True, False, 10.0, True),
    (False, True, 10.0, True),
    (False, False, ms.IDLE_AWAY_S, True),
])
def test_away_when_locked_asleep_or_idle(monkeypatch, locked, asleep, idle, away):
    monkeypatch.setattr(ms, 'screen_locked', lambda: locked)
    monkeypatch.setattr(ms, 'display_asleep', lambda: asleep)
    monkeypatch.setattr(ms, 'idle_seconds', lambda: idle)
    assert ms.user_away() is away


def test_a_failed_query_means_not_away(monkeypatch):
    def boom():
        raise OSError('no window server')
    monkeypatch.setattr(ms, 'screen_locked', boom)
    assert ms.user_away() is False


def test_the_oled_asks_macos_not_logind(monkeypatch):
    from arctis_sound_manager import desktop_session

    monkeypatch.setattr(desktop_session, 'IS_MACOS', True)
    monkeypatch.setattr(ms, 'user_away', lambda: True)
    session = desktop_session.DesktopSession()
    monkeypatch.setattr(session, '_run', lambda *a, **k: pytest.fail('D-Bus asked on macOS'))
    assert session.user_away() is True


@pytest.mark.skipif(sys.platform != 'darwin', reason='CoreGraphics is macOS only')
def test_presence_reads_the_real_session():
    assert isinstance(ms.screen_locked(), bool)
    assert isinstance(ms.display_asleep(), bool)
    assert ms.idle_seconds() >= 0

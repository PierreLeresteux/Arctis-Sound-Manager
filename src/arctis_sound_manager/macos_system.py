# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""What the Mac itself is doing, for the daemon: sleep, wake and presence.

On Linux the daemon hears about suspend from logind's PrepareForSleep, and
asks logind whether the session is locked or idle. macOS has no logind:

* **Sleep and wake** come from IOKit's system power notifications
  (IORegisterForSystemPower). They are delivered on a CFRunLoop, so
  SystemPowerWatcher runs one on its own thread and hands each event to the
  daemon's asyncio loop. macOS waits for every registered client to allow a
  sleep (30 s at most), which gives the daemon time to release the headset
  first, as on Linux.
* **Presence** is read on demand: the screen is locked (the login window's
  session dictionary), the display is asleep, or nobody has touched a key or
  the mouse for a while. Any of them means nobody is looking at the base
  station's screen.

Every call is over ctypes, with no dependency. Anything that fails answers
"not away" or leaves the watcher off: the daemon then behaves as it did
before.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import logging
import threading
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_IOKIT = '/System/Library/Frameworks/IOKit.framework/IOKit'
_CORE_FOUNDATION = '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation'
_CORE_GRAPHICS = '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics'

# IOKit/IOMessage.h: iokit_common_msg(n) = 0xE0000000 | n.
MSG_CAN_SYSTEM_SLEEP = 0xE0000270
MSG_SYSTEM_WILL_SLEEP = 0xE0000280
MSG_SYSTEM_WILL_NOT_SLEEP = 0xE0000290
MSG_SYSTEM_HAS_POWERED_ON = 0xE0000300

# How long the daemon gets to release the headset before the Mac sleeps.
# macOS itself gives up waiting after 30 s.
_SLEEP_PREPARE_TIMEOUT_S = 10.0

_RUN_SLICE_S = 1.0

# io_service_t service, natural_t messageType, void *messageArgument.
_POWER_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32,
                                   ctypes.c_uint32, ctypes.c_void_p)


def _load_power_libs():
    iokit = ctypes.CDLL(_IOKIT)
    cf = ctypes.CDLL(_CORE_FOUNDATION)
    iokit.IORegisterForSystemPower.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), _POWER_CALLBACK,
        ctypes.POINTER(ctypes.c_uint32)]
    iokit.IORegisterForSystemPower.restype = ctypes.c_uint32
    iokit.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
    iokit.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
    iokit.IOAllowPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    iokit.IOAllowPowerChange.restype = ctypes.c_int32
    iokit.IODeregisterForSystemPower.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    iokit.IODeregisterForSystemPower.restype = ctypes.c_int32
    iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
    iokit.IOServiceClose.restype = ctypes.c_int32
    iokit.IONotificationPortDestroy.argtypes = [ctypes.c_void_p]
    iokit.IONotificationPortDestroy.restype = None
    cf.CFRunLoopGetCurrent.argtypes = []
    cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    cf.CFRunLoopAddSource.restype = None
    cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
    cf.CFRunLoopRunInMode.restype = ctypes.c_int32
    return iokit, cf


class SystemPowerWatcher:
    """Calls *on_sleep* before the Mac sleeps and *on_wake* once it is back.

    *on_sleep* is a plain function, run on *loop* (the daemon's), and the
    sleep is allowed once it has returned. *on_wake* is a coroutine function,
    scheduled on *loop*. Both are the same CoreEngine methods logind's
    PrepareForSleep drives on Linux.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 on_sleep: Callable[[], None],
                 on_wake: Callable[[], Awaitable[None]],
                 libs=None) -> None:
        self._loop = loop
        self._on_sleep = on_sleep
        self._on_wake = on_wake
        self._libs = libs
        self._root_port = 0
        self._notify_port = ctypes.c_void_p()
        self._notifier = ctypes.c_uint32(0)
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        # Kept on the instance: IOKit holds a raw pointer to it.
        self._callback = _POWER_CALLBACK(self._on_message)

    def start(self, timeout: float = 5.0) -> bool:
        """Register with IOKit on a thread of its own; False if that failed."""
        self._thread = threading.Thread(target=self._run, name='macos-power', daemon=True)
        self._thread.start()
        self._started.wait(timeout)
        return bool(self._root_port)

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout)

    # ── the run loop thread ─────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            if self._libs is None:
                self._libs = _load_power_libs()
            iokit, cf = self._libs
            self._root_port = iokit.IORegisterForSystemPower(
                None, ctypes.byref(self._notify_port), self._callback,
                ctypes.byref(self._notifier))
            if not self._root_port:
                logger.warning("macOS power: IORegisterForSystemPower refused; "
                               "settings will not be re-applied after sleep")
                return
            source = iokit.IONotificationPortGetRunLoopSource(self._notify_port)
            mode = ctypes.c_void_p.in_dll(cf, 'kCFRunLoopDefaultMode')
            cf.CFRunLoopAddSource(cf.CFRunLoopGetCurrent(), source, mode)
        except Exception as e:
            logger.warning("macOS power: cannot watch sleep and wake: %r", e)
            self._root_port = 0
            return
        finally:
            self._started.set()

        logger.info("macOS power: watching sleep and wake")
        try:
            # In slices, so stop() is seen within a second whenever it comes.
            while not self._stopping.is_set():
                cf.CFRunLoopRunInMode(mode, _RUN_SLICE_S, False)
        finally:
            iokit.IODeregisterForSystemPower(ctypes.byref(self._notifier))
            iokit.IOServiceClose(self._root_port)
            iokit.IONotificationPortDestroy(self._notify_port)
            self._root_port = 0

    def _on_message(self, _refcon, _service, message: int, argument) -> None:
        try:
            self.handle(message, argument)
        except Exception as e:  # never let an exception cross into IOKit
            logger.warning("macOS power: %#x not handled: %r", message, e)

    def handle(self, message: int, argument) -> None:
        if message == MSG_CAN_SYSTEM_SLEEP:
            # Idle sleep asks first; the headset is released on the real one.
            self._allow(argument)
        elif message == MSG_SYSTEM_WILL_SLEEP:
            logger.info("macOS power: going to sleep")
            try:
                self._run_on_loop(self._on_sleep).result(_SLEEP_PREPARE_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                logger.warning("macOS power: the headset was not released within %.0f s",
                               _SLEEP_PREPARE_TIMEOUT_S)
            except Exception as e:
                logger.warning("macOS power: prepare_for_sleep failed: %r", e)
            finally:
                self._allow(argument)
        elif message == MSG_SYSTEM_HAS_POWERED_ON:
            logger.info("macOS power: awake, re-applying the headset settings")
            asyncio.run_coroutine_threadsafe(self._wake(), self._loop)
        elif message == MSG_SYSTEM_WILL_NOT_SLEEP:
            logger.info("macOS power: sleep cancelled")

    def _allow(self, argument) -> None:
        if self._root_port and self._libs is not None:
            self._libs[0].IOAllowPowerChange(self._root_port, argument)

    def _run_on_loop(self, fn: Callable[[], None]) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()

        def call() -> None:
            try:
                future.set_result(fn())
            except Exception as e:
                future.set_exception(e)

        self._loop.call_soon_threadsafe(call)
        return future

    async def _wake(self) -> None:
        try:
            await self._on_wake()
        except Exception as e:
            logger.warning("macOS power: resume_from_sleep failed: %r", e)


# ── presence ────────────────────────────────────────────────────────────────

#: No keyboard or mouse input for this long counts as away: GNOME's default
#: idle delay, which sets logind's IdleHint on Linux.
IDLE_AWAY_S = 300.0

_CG_HID_SYSTEM_STATE = 1  # kCGEventSourceStateHIDSystemState
_CG_ANY_INPUT_EVENT = 0xFFFFFFFF  # kCGAnyInputEventType
_CF_UTF8 = 0x08000100
_PRESENCE_LIBS = None


def _presence_libs():
    global _PRESENCE_LIBS
    if _PRESENCE_LIBS is None:
        cg = ctypes.CDLL(_CORE_GRAPHICS)
        cf = ctypes.CDLL(_CORE_FOUNDATION)
        cg.CGSessionCopyCurrentDictionary.argtypes = []
        cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        cg.CGMainDisplayID.argtypes = []
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
        cg.CGDisplayIsAsleep.restype = ctypes.c_bool
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        locked_key = cf.CFStringCreateWithCString(None, b'CGSSessionScreenIsLocked', _CF_UTF8)
        _PRESENCE_LIBS = cg, cf, locked_key
    return _PRESENCE_LIBS


def screen_locked() -> bool:
    """Whether the login session's screen is locked."""
    cg, cf, key = _presence_libs()
    session = cg.CGSessionCopyCurrentDictionary()
    if not session:
        # No window server session: nobody can be looking.
        return False
    try:
        value = cf.CFDictionaryGetValue(session, key)
        return bool(value) and cf.CFBooleanGetValue(value)
    finally:
        cf.CFRelease(session)


def display_asleep() -> bool:
    cg, _cf, _key = _presence_libs()
    return bool(cg.CGDisplayIsAsleep(cg.CGMainDisplayID()))


def idle_seconds() -> float:
    """Seconds since the last keyboard, mouse or trackpad input."""
    cg, _cf, _key = _presence_libs()
    return float(cg.CGEventSourceSecondsSinceLastEventType(
        _CG_HID_SYSTEM_STATE, _CG_ANY_INPUT_EVENT))


def user_away(idle_after: float = IDLE_AWAY_S) -> bool:
    """True when the screen is locked, the display asleep, or nobody has
    touched the Mac for *idle_after* seconds."""
    try:
        return screen_locked() or display_asleep() or idle_seconds() >= idle_after
    except Exception as e:
        logger.debug("macOS presence query failed: %r", e)
        return False

# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""The few CoreAudio calls the macOS software equaliser needs, over ctypes.

Listing the devices, and reading and changing the system's default output
and input: soft_eq moves them to BlackHole while it runs, and back after.
BlackHole applies the system volume itself, so the volume keys keep
working with nothing to do here. macOS only; every call raises
CoreAudioError rather than returning garbage.
"""

from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass


class CoreAudioError(OSError):
    pass


def _fourcc(code: str) -> int:
    return struct.unpack('>I', code.encode('ascii'))[0]


_SYSTEM_OBJECT = 1
_SCOPE_GLOBAL = _fourcc('glob')
_SCOPE_OUTPUT = _fourcc('outp')
_SCOPE_INPUT = _fourcc('inpt')
_ELEMENT_MAIN = 0
_DEVICES = _fourcc('dev#')
_DEFAULT_OUTPUT = _fourcc('dOut')
_DEFAULT_INPUT = _fourcc('dIn ')
_NAME = _fourcc('lnam')
_UID = _fourcc('uid ')
_STREAM_CONFIGURATION = _fourcc('slay')
_CF_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [('selector', ctypes.c_uint32), ('scope', ctypes.c_uint32),
                ('element', ctypes.c_uint32)]


@dataclass(frozen=True)
class AudioDevice:
    id: int
    name: str
    uid: str
    #: Channels on the side it was listed for (output or input).
    channels: int


_LIBS = None


def _libs():
    global _LIBS
    if _LIBS is None:
        # By path: find_library searches differently inside a frozen app.
        ca = ctypes.CDLL('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
        cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        address = ctypes.POINTER(_Address)
        ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        ca.AudioObjectSetPropertyData.argtypes = [
            ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p]
        for fn in ('AudioObjectGetPropertyDataSize', 'AudioObjectGetPropertyData',
                   'AudioObjectSetPropertyData'):
            getattr(ca, fn).restype = ctypes.c_int32
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                                          ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _LIBS = ca, cf
    return _LIBS


def _get(object_id: int, selector: int, scope: int = _SCOPE_GLOBAL,
         element: int = _ELEMENT_MAIN) -> bytes:
    ca, _cf = _libs()
    address = _Address(selector, scope, element)
    size = ctypes.c_uint32(0)
    status = ca.AudioObjectGetPropertyDataSize(object_id, ctypes.byref(address), 0, None,
                                               ctypes.byref(size))
    if status:
        raise CoreAudioError(status, f"size of {selector:#x} on {object_id}")
    buffer = ctypes.create_string_buffer(size.value)
    status = ca.AudioObjectGetPropertyData(object_id, ctypes.byref(address), 0, None,
                                           ctypes.byref(size), buffer)
    if status:
        raise CoreAudioError(status, f"read {selector:#x} on {object_id}")
    return buffer.raw[:size.value]


def _set(object_id: int, selector: int, data: bytes, scope: int = _SCOPE_GLOBAL,
         element: int = _ELEMENT_MAIN) -> None:
    ca, _cf = _libs()
    address = _Address(selector, scope, element)
    buffer = ctypes.create_string_buffer(data, len(data))
    status = ca.AudioObjectSetPropertyData(object_id, ctypes.byref(address), 0, None,
                                           len(data), buffer)
    if status:
        raise CoreAudioError(status, f"write {selector:#x} on {object_id}")


def _string(object_id: int, selector: int) -> str:
    _ca, cf = _libs()
    ref = ctypes.c_void_p.from_buffer_copy(_get(object_id, selector)).value
    if not ref:
        return ''
    try:
        out = ctypes.create_string_buffer(1024)
        if not cf.CFStringGetCString(ref, out, len(out), _CF_UTF8):
            return ''
        return out.value.decode('utf-8', 'replace')
    finally:
        cf.CFRelease(ref)


def _channels(device_id: int, scope: int) -> int:
    """Channels across the device's streams on one side (an AudioBufferList)."""
    raw = _get(device_id, _STREAM_CONFIGURATION, scope)
    if len(raw) < 4:
        return 0
    buffers = struct.unpack_from('I', raw)[0]
    # mNumberBuffers, padded to 8, then 16-byte AudioBuffers whose first
    # field is mNumberChannels.
    return sum(struct.unpack_from('I', raw, 8 + 16 * i)[0] for i in range(buffers)
               if 8 + 16 * i + 4 <= len(raw))


def devices(side: str = 'output') -> list[AudioDevice]:
    """The devices with channels on *side*, 'output' or 'input'."""
    scope = _SCOPE_OUTPUT if side == 'output' else _SCOPE_INPUT
    raw = _get(_SYSTEM_OBJECT, _DEVICES)
    out = []
    for device_id in struct.unpack(f'{len(raw) // 4}I', raw):
        try:
            channels = _channels(device_id, scope)
            if channels:
                out.append(AudioDevice(device_id, _string(device_id, _NAME),
                                       _string(device_id, _UID), channels))
        except CoreAudioError:
            continue
    return out


def default_device(side: str = 'output') -> int:
    selector = _DEFAULT_OUTPUT if side == 'output' else _DEFAULT_INPUT
    return struct.unpack('I', _get(_SYSTEM_OBJECT, selector))[0]


def set_default_device(side: str, device_id: int) -> None:
    selector = _DEFAULT_OUTPUT if side == 'output' else _DEFAULT_INPUT
    _set(_SYSTEM_OBJECT, selector, struct.pack('I', device_id))


def output_devices() -> list[AudioDevice]:
    return devices('output')


# ── Speaker layout ────────────────────────────────────────────────────────────

_PREFERRED_LAYOUT = _fourcc('srnd')
_LAYOUT_FOR_TAG = _fourcc('cmpl')
_LAYOUT_FOR_BITMAP = _fourcc('cmpb')
_USE_DESCRIPTIONS = 0
_USE_BITMAP = 1 << 16
_TOOLBOX = None


def _toolbox():
    global _TOOLBOX
    if _TOOLBOX is None:
        tb = ctypes.CDLL('/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox')
        tb.AudioFormatGetPropertyInfo.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        tb.AudioFormatGetProperty.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p]
        tb.AudioFormatGetPropertyInfo.restype = ctypes.c_int32
        tb.AudioFormatGetProperty.restype = ctypes.c_int32
        _TOOLBOX = tb
    return _TOOLBOX


def _expand_layout(selector: int, specifier: int) -> bytes:
    """A layout tag or bitmap spelled out as channel descriptions (AudioToolbox)."""
    tb = _toolbox()
    spec = ctypes.c_uint32(specifier)
    size = ctypes.c_uint32(0)
    status = tb.AudioFormatGetPropertyInfo(selector, 4, ctypes.byref(spec), ctypes.byref(size))
    if status:
        raise CoreAudioError(status, f"size of layout {specifier:#x}")
    buffer = ctypes.create_string_buffer(size.value)
    status = tb.AudioFormatGetProperty(selector, 4, ctypes.byref(spec), ctypes.byref(size), buffer)
    if status:
        raise CoreAudioError(status, f"layout {specifier:#x}")
    return buffer.raw[:size.value]


def parse_layout(raw: bytes, expand=_expand_layout) -> list[int]:
    """The channel label of each channel of an AudioChannelLayout, in order.

    A layout comes as one description per channel, as a bitmap of the
    speakers present, or as a predefined tag; the last two are expanded
    to descriptions the way CoreAudio does.
    """
    if len(raw) < 12:
        return []
    tag, bitmap, count = struct.unpack_from('III', raw)
    if tag == _USE_BITMAP:
        raw = expand(_LAYOUT_FOR_BITMAP, bitmap)
    elif tag != _USE_DESCRIPTIONS:
        raw = expand(_LAYOUT_FOR_TAG, tag)
    if len(raw) < 12:
        return []
    count = struct.unpack_from('I', raw, 8)[0]
    # 12-byte header, then 20-byte descriptions whose first field is the label.
    return [struct.unpack_from('I', raw, 12 + 20 * i)[0] for i in range(count)
            if 12 + 20 * i + 4 <= len(raw)]


def channel_layout(device_id: int) -> list[int]:
    """The output speaker layout set for a device in Audio MIDI Setup
    (Configure Speakers), as one CoreAudio channel label per channel."""
    return parse_layout(_get(device_id, _PREFERRED_LAYOUT, _SCOPE_OUTPUT))

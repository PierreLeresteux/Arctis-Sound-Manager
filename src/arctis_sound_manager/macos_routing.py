# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Mac's default output, moved when the headset comes and goes.

On Linux, "Redirect audio on connect" hands the system default to one of
ASM's channels and "redirect on disconnect" hands it to a device the user
picked, both through PipeWire. On macOS there are no channels: the default
output goes to the headset itself, or to the software equaliser's BlackHole
while the equaliser runs, and back to the chosen device (or the Mac's own
speakers) when the headset goes off.

Devices are saved by CoreAudio UID, which survives a replug and a reboot;
a name is matched too, for the equaliser's "Play on", which stores names.
"""

from __future__ import annotations

import logging

from arctis_sound_manager import coreaudio
from arctis_sound_manager.soft_eq import LOOPBACKS, pick_output

logger = logging.getLogger(__name__)

_HEADSET_WORDS = ('arctis', 'steelseries')
_VIRTUAL = int.from_bytes(b'virt', 'big')  # kAudioDeviceTransportTypeVirtual


def _find(devices: list[coreaudio.AudioDevice], wanted: str) -> coreaudio.AudioDevice | None:
    if not wanted:
        return None
    return next((d for d in devices if wanted in (d.uid, d.name)), None)


def choices(side: str = 'output', ca=coreaudio) -> list[dict[str, str]]:
    """The devices a setting can name, for the GUI's pickers: every device
    on *side* but the equaliser's own BlackHoles. Both label keys, since
    pickers read one or the other."""
    try:
        devices = ca.devices(side)
    except OSError as exc:
        logger.warning('could not list the %s devices: %r', side, exc)
        return []
    return [{'id': d.uid, 'name': d.name, 'description': d.name}
            for d in devices if d.name not in LOOPBACKS]


def resolve(wanted: str, side: str = 'output', ca=coreaudio) -> str | None:
    """The name of the device a setting names (UID or name), when plugged in."""
    try:
        found = _find([d for d in ca.devices(side) if d.name not in LOOPBACKS], wanted)
    except OSError as exc:
        logger.debug('could not resolve %r: %r', wanted, exc)
        return None
    return found.name if found else None


def _headset_side(device: coreaudio.AudioDevice, also: tuple[str, ...]) -> bool:
    """Whether sound sent to *device* goes to the headset: the headset
    itself, the equaliser's BlackHole in front of it, or the device generic
    mode plays on."""
    name = device.name.lower()
    return (device.name in LOOPBACKS or any(w in name for w in _HEADSET_WORDS)
            or device.name in also or device.uid in also)


def take_default(loopback: str, wanted: str, ca=coreaudio) -> str | None:
    """Make the headset the Mac's output, as it comes online.

    *loopback* is the BlackHole the equaliser reads while it runs: sound must
    go there, not straight to the headset, or it would skip the EQ.
    Otherwise the headset's output, or *wanted* (the equaliser's "Play on",
    or generic mode's device). Returns the new output's name, None when
    nothing changed.
    """
    devices = ca.devices('output')
    target = (next((d for d in devices if d.name == loopback), None) if loopback
              else pick_output(devices, wanted))
    if target is None or ca.default_device('output') == target.id:
        return None
    ca.set_default_device('output', target.id)
    logger.info('Headset online: Mac output -> %s', target.name)
    return target.name


def leave_default(wanted: str, also: tuple[str, ...] = (), ca=coreaudio) -> str | None:
    """Move the Mac's output off the headset, as it goes offline.

    Only when the output is the headset's (see _headset_side): an output the
    user already moved elsewhere stays. To *wanted* when it is plugged in,
    else the Mac's own speakers, else any real device. Returns the new
    output's name, None when nothing changed.
    """
    devices = ca.devices('output')
    current = next((d for d in devices if d.id == ca.default_device('output')), None)
    if current is None or not _headset_side(current, also):
        return None
    others = [d for d in devices if not _headset_side(d, also)]
    target = _find(others, wanted)
    if target is None:
        if wanted:
            logger.info('Redirect on disconnect: %r is not plugged in, using another output',
                        wanted)
        kinds = {d.id: ca.transport_type(d.id) for d in others}
        real = [d for d in others if kinds[d.id] != _VIRTUAL]
        built_in = [d for d in real if kinds[d.id] == coreaudio.TRANSPORT_BUILT_IN]
        target = (built_in or real or [None])[0]
    if target is None:
        logger.warning('Headset offline, but no other output to move the Mac to')
        return None
    ca.set_default_device('output', target.id)
    logger.info('Headset offline: Mac output -> %s', target.name)
    return target.name

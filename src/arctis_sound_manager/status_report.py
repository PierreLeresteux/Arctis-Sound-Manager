# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""
`asm-cli status` — the headset's state in one call, for scripts and bars.

What HeadsetControl's `-o json` is to its users: a stable shape a Waybar /
Polybar / i3blocks module, a GNOME or KDE widget, or a shell script can read
without speaking D-Bus. Everything comes from the running daemon (it holds the
USB interface, nothing else may talk to the headset) plus PipeWire for the
channel levels.

Kept free of core.py / Qt imports on purpose: a bar module runs this every few
seconds, and importing the engine would cost more than the call itself.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from arctis_sound_manager.platform_support import AUDIO_SUPPORTED
from arctis_sound_manager.power_status import HeadsetPower, extract_power_status

# Bumped only when a key is removed or changes meaning. Adding keys is not a
# breaking change and does not bump it.
SCHEMA_VERSION = 1


def _call_daemon(object_path: str, interface: str, member: str,
                 timeout: float) -> Any:
    """Call a JSON-returning daemon method. Returns None when unreachable."""
    from dbus_next.aio.message_bus import MessageBus
    from dbus_next.constants import MessageType
    from dbus_next.message import Message

    from arctis_sound_manager.constants import DBUS_BUS_NAME

    async def _call() -> Any:
        bus = None
        try:
            bus = await asyncio.wait_for(MessageBus().connect(), timeout=timeout)
            reply = await asyncio.wait_for(bus.call(Message(
                destination=DBUS_BUS_NAME,
                path=object_path,
                interface=interface,
                member=member,
                message_type=MessageType.METHOD_CALL,
            )), timeout=timeout)
            if reply is None or reply.message_type == MessageType.ERROR:
                return None
            return json.loads(reply.body[0])
        except Exception:
            return None
        finally:
            if bus is not None:
                bus.disconnect()

    return asyncio.run(_call())


def _channels() -> dict[str, dict[str, Any]] | None:
    """Level and mute of each ASM channel sink, or None if PipeWire is unreachable."""
    try:
        import pulsectl

        from arctis_sound_manager.channel_control import (CHANNELS,
                                                           OPTIONAL_CHANNELS,
                                                           _find_sink)
    except Exception:
        return None

    channels: dict[str, dict[str, Any]] = {}
    try:
        with pulsectl.Pulse("asm-status") as pulse:
            for channel, node_name in CHANNELS.items():
                sink = _find_sink(pulse, node_name)
                if sink is None:
                    # An optional channel nobody switched on is not a fault;
                    # leave it out rather than report it absent (#209).
                    if channel not in OPTIONAL_CHANNELS:
                        channels[channel] = {'present': False}
                    continue
                channels[channel] = {
                    'present': True,
                    'volume': round(pulse.volume_get_all_chans(sink) * 100),
                    'muted': bool(sink.mute),
                }
    except Exception:
        return None
    return channels


def collect_status(timeout: float = 2.0) -> dict[str, Any]:
    """Build the `asm-cli status --json` payload. Never raises."""
    from arctis_sound_manager.constants import (DBUS_BUS_NAME,
                                                 DBUS_SETTINGS_OBJECT_PATH,
                                                 DBUS_STATUS_OBJECT_PATH)

    status = _call_daemon(DBUS_STATUS_OBJECT_PATH, f'{DBUS_BUS_NAME}.Status',
                          'GetStatus', timeout)
    result: dict[str, Any] = {
        'schema': SCHEMA_VERSION,
        'daemon': status is not None,
        'device': None,
        'power': HeadsetPower.UNKNOWN.value,
        'status': {},
        'channels': _channels(),
    }
    if status is None:
        return result

    settings = _call_daemon(DBUS_SETTINGS_OBJECT_PATH, f'{DBUS_BUS_NAME}.Settings',
                            'GetSettings', timeout) or {}
    if settings.get('device_name'):
        result['device'] = {
            'name': settings['device_name'],
            'vendor_id': settings.get('vendor_id', ''),
            'product_id': settings.get('product_id', ''),
        }

    result['power'] = extract_power_status(status).value
    # Flattened: GetStatus groups variables by GUI section, which is layout,
    # not data. A script wants `.status.headset_battery_charge`, not to know
    # which tab of the window it happens to be shown on.
    for variables in status.values():
        if isinstance(variables, dict):
            for name, entry in variables.items():
                if isinstance(entry, dict) and 'value' in entry:
                    result['status'][name] = entry['value']
    return result


def format_status(result: dict[str, Any]) -> list[str]:
    """Human-readable rendering of collect_status()."""
    if not result['daemon']:
        return ['asm-daemon: not running']

    device = result['device']
    lines = [f"device  {device['name'] if device else 'none detected'}",
             f"power   {result['power']}"]
    for name, value in result['status'].items():
        if name == 'headset_power_status':
            continue
        lines.append(f"  {name}: {value}")

    channels = result['channels']
    if channels is None:
        if AUDIO_SUPPORTED:
            lines.append('channels: PipeWire unreachable')
    else:
        for channel, info in channels.items():
            if not info['present']:
                lines.append(f"{channel:6s}  absent")
            else:
                lines.append(f"{channel:6s}  {info['volume']:3d}%"
                             + ("  muted" if info['muted'] else ""))
    return lines

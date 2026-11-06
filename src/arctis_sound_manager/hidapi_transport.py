# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""Talk to the headset through hidapi, on macOS.

macOS keeps every HID interface for its own HID manager: libusb cannot
detach it or claim the interface, so neither of the Linux paths applies.
hidapi goes through IOHIDManager instead, which shares the interface with
the system, much like hidraw shares it with the kernel driver on Linux.

The class mirrors HidrawTransport call for call (write_output, send_feature,
get_feature, read), so the code in core.py and oled_manager.py that already
drives hidraw drives this one unchanged. hidapi also follows hidraw's report
number convention: the first byte is the report id, 0 on an interface
without numbered reports. Which kind an interface is comes from its report
descriptor, as on hidraw.

hidapi's own errors carry no errno: they are raised as
usb.core.USBError(errno=EIO), or ENODEV once the device has left, so the
listen loop and the command path see what they see on Linux.
"""

from __future__ import annotations

import errno
import logging
from typing import Iterable

import usb.core

from arctis_sound_manager.hidraw_transport import descriptor_uses_report_ids

logger = logging.getLogger(__name__)

# The pages SteelSeries uses for its command interfaces start here.
_VENDOR_USAGE_PAGE = 0xFF00


def _hid():
    import hid
    return hid


def find_hidapi_paths(vendor_id: int, product_id: int, interfaces: Iterable[int],
                      usage_page: int | None = None) -> dict[int, bytes]:
    """bInterfaceNumber → hidapi path for the interfaces asked for.

    hidapi lists one entry per top-level collection, so an interface can
    appear more than once. With *usage_page* given, an interface is taken
    only through an entry on that page; otherwise a vendor page wins over
    the standard ones (consumer control, telephony).
    """
    wanted = {i for i in interfaces if i >= 0}
    best: dict[int, tuple[bool, bytes]] = {}
    for entry in _hid().enumerate(vendor_id, product_id):
        iface = entry.get('interface_number', -1)
        if iface not in wanted:
            continue
        page = entry.get('usage_page', 0)
        if usage_page is not None and page != usage_page:
            continue
        vendor = page >= _VENDOR_USAGE_PAGE
        if iface not in best or (vendor and not best[iface][0]):
            best[iface] = (vendor, entry['path'])
    return {iface: path for iface, (_vendor, path) in best.items()}


class HidapiTransport:
    """Open hidapi devices for one headset, keyed by USB interface number."""

    def __init__(self, paths: dict[int, bytes], vendor_id: int, product_id: int):
        self.nodes = {iface: path.decode(errors='replace') if isinstance(path, bytes) else str(path)
                      for iface, path in paths.items()}
        self._paths = dict(paths)
        self._vendor_id = vendor_id
        self._product_id = product_id
        self._devices: dict[int, object] = {}
        self._numbered: dict[int, bool] = {}
        try:
            for iface, path in self._paths.items():
                device = _hid().device()
                device.open_path(path)
                self._devices[iface] = device
                self._numbered[iface] = self._read_numbered(device, iface)
        except (OSError, ValueError) as e:
            self.close()
            raise OSError(errno.EIO, f"cannot open {self.nodes.get(iface)}: {e}") from e

    @staticmethod
    def _read_numbered(device, iface: int) -> bool:
        try:
            return descriptor_uses_report_ids(bytes(device.get_report_descriptor()))
        except (OSError, ValueError, AttributeError) as e:
            # Same default as hidraw: ASM's frames then go out exactly as the
            # libusb path sent them.
            logger.warning("hidapi: cannot read the report descriptor of interface %d (%s)", iface, e)
            return True

    def has(self, iface: int) -> bool:
        return iface in self._devices

    def uses_report_ids(self, iface: int) -> bool:
        return self._numbered.get(iface, True)

    def _device(self, iface: int):
        try:
            return self._devices[iface]
        except KeyError:
            raise usb.core.USBError(f"no hidapi device for interface {iface}",
                                    errno=errno.ENOENT) from None

    def _framed(self, iface: int, data: list[int] | bytes) -> bytes:
        """ASM's frame with the report number hidapi expects in front."""
        data = bytes(b & 0xFF for b in data)
        return data if self.uses_report_ids(iface) else b'\x00' + data

    def _error(self, what: str, e: Exception) -> usb.core.USBError:
        gone = not any(entry.get('path') in self._paths.values()
                       for entry in _hid().enumerate(self._vendor_id, self._product_id))
        code = errno.ENODEV if gone else errno.EIO
        return usb.core.USBError(f"hidapi {what} failed: {e}", errno=code)

    def write_output(self, iface: int, data: list[int] | bytes) -> int:
        device = self._device(iface)
        try:
            written = device.write(self._framed(iface, data))
        except (OSError, ValueError) as e:
            raise self._error('write', e) from e
        if written < 0:
            raise self._error('write', OSError(device.error()))
        return written

    def send_feature(self, iface: int, data: list[int] | bytes) -> None:
        device = self._device(iface)
        try:
            sent = device.send_feature_report(self._framed(iface, data))
        except (OSError, ValueError) as e:
            raise self._error('send_feature_report', e) from e
        if sent < 0:
            raise self._error('send_feature_report', OSError(device.error()))

    def get_feature(self, iface: int, report_id: int, size: int) -> list[int]:
        """A feature report as GET_REPORT returns it: report id first when
        the interface numbers its reports, `size` bytes at most."""
        device = self._device(iface)
        try:
            data = bytes(device.get_feature_report(report_id & 0xFF, max(size, 1)))
        except (OSError, ValueError) as e:
            raise self._error('get_feature_report', e) from e
        return list(data if self.uses_report_ids(iface) else data[1:])

    def read(self, iface: int, size: int, timeout_ms: int) -> list[int]:
        device = self._device(iface)
        try:
            # A whole report, whatever the endpoint size, as on hidraw.
            data = device.read(max(size, 4096), timeout_ms)
        except (OSError, ValueError) as e:
            raise self._error('read', e) from e
        if not data:
            raise usb.core.USBError("Operation timed out", errno=errno.ETIMEDOUT)
        return list(data)

    def close(self) -> None:
        for device in self._devices.values():
            try:
                device.close()
            except (OSError, ValueError):
                pass
        self._devices.clear()

    def describe(self) -> str:
        """One line per interface, for the log and the bug report."""
        return ", ".join(
            f"interface {iface} → {node} ({'numbered' if self.uses_report_ids(iface) else 'unnumbered'} reports)"
            for iface, node in sorted(self.nodes.items()))

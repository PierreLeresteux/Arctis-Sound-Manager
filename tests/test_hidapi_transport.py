# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The hidapi transport (macOS): the same calls as the hidraw transport, over
IOHIDManager through hidapi. A fake `hid` module stands in for the device,
so these run anywhere.
"""

from __future__ import annotations

import errno
import sys
import types

import pytest
import usb.core

from arctis_sound_manager.hidapi_transport import HidapiTransport, find_hidapi_paths

NUMBERED = [0x06, 0xC0, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x06, 0x95, 0x3F, 0xC0]
UNNUMBERED = [0x06, 0xC0, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0x95, 0x40, 0x75, 0x08, 0xC0]


class FakeDevice:
    def __init__(self, hid):
        self.hid = hid
        self.path = None
        self.closed = False

    def open_path(self, path):
        if path not in self.hid.descriptors:
            raise OSError("open failed")
        self.path = path

    def get_report_descriptor(self):
        return self.hid.descriptors[self.path]

    def write(self, data):
        self.hid.written.append(bytes(data))
        return len(data)

    def send_feature_report(self, data):
        self.hid.features.append(bytes(data))
        return len(data)

    def get_feature_report(self, report_id, size):
        return [report_id] + [0xAB] * (size - 1)

    def read(self, size, timeout_ms):
        if self.hid.read_error:
            raise OSError("read error")
        return self.hid.reads.pop(0) if self.hid.reads else []

    def error(self):
        return "error"

    def close(self):
        self.closed = True


@pytest.fixture
def fake_hid(monkeypatch):
    hid = types.ModuleType("hid")
    hid.descriptors = {b"if0": UNNUMBERED, b"if3": NUMBERED}
    hid.entries = [
        {"interface_number": 0, "usage_page": 0x000C, "path": b"if0-consumer"},
        {"interface_number": 0, "usage_page": 0xFFC0, "path": b"if0"},
        {"interface_number": 3, "usage_page": 0xFFC0, "path": b"if3"},
        {"interface_number": 4, "usage_page": 0xFFC0, "path": b"if4"},
    ]
    hid.written, hid.features, hid.reads, hid.read_error = [], [], [], False
    hid.enumerate = lambda vid, pid: list(hid.entries)
    hid.device = lambda: FakeDevice(hid)
    monkeypatch.setitem(sys.modules, "hid", hid)
    return hid


def test_paths_prefer_the_vendor_collection(fake_hid):
    assert find_hidapi_paths(0x1038, 0x12C2, [0, 3, -1]) == {0: b"if0", 3: b"if3"}


def test_paths_follow_a_declared_usage_page(fake_hid):
    assert find_hidapi_paths(0x1038, 0x12C2, [0], usage_page=0x000C) == {0: b"if0-consumer"}


def test_unnumbered_reports_get_a_zero_in_front(fake_hid):
    transport = HidapiTransport({0: b"if0"}, 0x1038, 0x12C2)

    transport.write_output(0, [0x06, 0x00, 0xC0])
    transport.send_feature(0, [0x90, 0x00])

    assert fake_hid.written == [b"\x00\x06\x00\xc0"]
    assert fake_hid.features == [b"\x00\x90\x00"]
    assert transport.get_feature(0, 0, 4) == [0xAB, 0xAB, 0xAB]


def test_numbered_reports_go_out_as_they_are(fake_hid):
    transport = HidapiTransport({3: b"if3"}, 0x1038, 0x12C2)

    transport.write_output(3, [0x06, 0x01])

    assert fake_hid.written == [b"\x06\x01"]
    assert transport.get_feature(3, 0x06, 3) == [0x06, 0xAB, 0xAB]


def test_empty_read_is_a_timeout(fake_hid):
    transport = HidapiTransport({0: b"if0"}, 0x1038, 0x12C2)
    fake_hid.reads = [[0xAA, 0x03, 0x01]]

    assert transport.read(0, 64, 1000) == [0xAA, 0x03, 0x01]
    with pytest.raises(usb.core.USBError) as exc:
        transport.read(0, 64, 1000)
    assert exc.value.errno == errno.ETIMEDOUT


def test_read_error_is_enodev_once_the_device_left(fake_hid):
    transport = HidapiTransport({0: b"if0"}, 0x1038, 0x12C2)
    fake_hid.read_error = True

    with pytest.raises(usb.core.USBError) as exc:
        transport.read(0, 64, 1000)
    assert exc.value.errno == errno.EIO

    fake_hid.entries = []
    with pytest.raises(usb.core.USBError) as exc:
        transport.read(0, 64, 1000)
    assert exc.value.errno == errno.ENODEV


def test_missing_interface_and_failed_open(fake_hid):
    transport = HidapiTransport({0: b"if0"}, 0x1038, 0x12C2)
    assert transport.has(0) and not transport.has(3)
    with pytest.raises(usb.core.USBError) as exc:
        transport.write_output(3, [0x00])
    assert exc.value.errno == errno.ENOENT

    with pytest.raises(OSError):
        HidapiTransport({0: b"if0", 4: b"if4"}, 0x1038, 0x12C2)

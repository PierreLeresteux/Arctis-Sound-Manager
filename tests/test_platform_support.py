# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The platform helpers: the macOS session bus address and notifications.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from arctis_sound_manager import platform_support


def _on_macos(monkeypatch, launchctl_socket: str = ""):
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=launchctl_socket + "\n", returncode=0)

    monkeypatch.setattr(platform_support, "IS_MACOS", True)
    monkeypatch.setattr(platform_support, "_patch_dbus_next_partial_writes", lambda: None)
    monkeypatch.setattr(platform_support.subprocess, "run", run)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("DBUS_LAUNCHD_SESSION_BUS_SOCKET", raising=False)
    return calls


def test_launchd_socket_becomes_a_unix_address(monkeypatch):
    _on_macos(monkeypatch, "/private/tmp/com.apple.launchd.x/unix_domain_listener")

    platform_support.prepare_session_bus_env()

    assert (platform_support.os.environ["DBUS_SESSION_BUS_ADDRESS"]
            == "unix:path=/private/tmp/com.apple.launchd.x/unix_domain_listener")


def test_launchd_address_is_replaced(monkeypatch):
    _on_macos(monkeypatch, "/tmp/sock")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "launchd:env=DBUS_LAUNCHD_SESSION_BUS_SOCKET")

    platform_support.prepare_session_bus_env()

    assert platform_support.os.environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/sock"


def test_usable_address_is_kept(monkeypatch):
    calls = _on_macos(monkeypatch, "/tmp/sock")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/other")

    platform_support.prepare_session_bus_env()

    assert platform_support.os.environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/other"
    assert calls == []


def test_no_bus_leaves_the_environment_alone(monkeypatch):
    _on_macos(monkeypatch, "")

    platform_support.prepare_session_bus_env()

    assert "DBUS_SESSION_BUS_ADDRESS" not in platform_support.os.environ


def test_linux_is_untouched(monkeypatch):
    calls = _on_macos(monkeypatch)
    monkeypatch.setattr(platform_support, "IS_MACOS", False)

    platform_support.prepare_session_bus_env()

    assert calls == []
    assert "DBUS_SESSION_BUS_ADDRESS" not in platform_support.os.environ


def test_macos_notification_quotes_its_text(monkeypatch):
    calls = _on_macos(monkeypatch)

    platform_support.notify('Say "hi"', "back\\slash")

    assert calls == [["osascript", "-e",
                      'display notification "back\\\\slash" with title "Say \\"hi\\""']]


def test_missing_notifier_is_silent(monkeypatch):
    monkeypatch.setattr(platform_support, "IS_MACOS", False)
    monkeypatch.setattr(platform_support.shutil, "which", lambda name: None)

    def run(*args, **kwargs):
        raise AssertionError("nothing to run")

    monkeypatch.setattr(platform_support.subprocess, "run", run)
    platform_support.notify("t", "b")


def test_full_send_buffer_waits_instead_of_closing_the_bus(monkeypatch):
    import queue

    from dbus_next.aio import message_bus

    monkeypatch.setattr(message_bus._MessageWriter, "write_callback",
                        message_bus._MessageWriter.write_callback)
    monkeypatch.delattr(message_bus._MessageWriter, "_asm_patched", raising=False)
    platform_support._patch_dbus_next_partial_writes()

    class Sock:
        def __init__(self):
            self.sent = b""
            self.room = 3

        def send(self, data):
            if not self.room:
                raise BlockingIOError(35, "Resource temporarily unavailable")
            chunk = bytes(data[:self.room])
            self.room -= len(chunk)
            self.sent += chunk
            return len(chunk)

    class Future:
        result = None

        def done(self):
            return self.result is not None

        def set_result(self, value):
            self.result = "ok"

    writer = message_bus._MessageWriter.__new__(message_bus._MessageWriter)
    writer.messages = queue.Queue()
    writer.sock = Sock()
    writer.loop = SimpleNamespace(remove_writer=lambda fd: None)
    writer.bus = SimpleNamespace(_finalize=lambda e: (_ for _ in ()).throw(AssertionError(e)))
    writer.negotiate_unix_fd = False
    writer.fd, writer.buf, writer.offset, writer.unix_fds, writer.fut = 3, None, 0, None, None
    fut = Future()
    writer.messages.put_nowait((b"hello", [], fut))

    writer.write_callback()          # 3 bytes out, then the buffer is full
    assert writer.sock.sent == b"hel" and fut.result is None

    writer.sock.room = 10            # drained: the loop calls back
    writer.write_callback()
    assert writer.sock.sent == b"hello" and fut.result == "ok"


def _daemon_start(monkeypatch, tmp_path):
    started: list[list[str]] = []

    class Popen:
        def __init__(self, command, **kwargs):
            assert kwargs["start_new_session"]
            started.append(command)

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(platform_support.subprocess, "Popen", Popen)
    monkeypatch.setattr(platform_support.sys, "executable", str(tmp_path / "MacOS" / "asm-gui"))
    (tmp_path / "MacOS").mkdir()
    (tmp_path / "MacOS" / "asm-daemon").touch()
    return started


def test_app_starts_the_daemon_next_to_it(monkeypatch, tmp_path):
    started = _daemon_start(monkeypatch, tmp_path)
    (tmp_path / "run").mkdir()

    platform_support.start_daemon_if_needed()

    assert started == [[str(tmp_path / "MacOS" / "asm-daemon")]]
    assert (tmp_path / "Library" / "Logs" / "asm-daemon.log").exists()


def test_app_starts_the_daemon_before_any_runtime_dir(monkeypatch, tmp_path):
    started = _daemon_start(monkeypatch, tmp_path)

    platform_support.start_daemon_if_needed()

    assert len(started) == 1


def test_app_leaves_a_running_daemon_alone(monkeypatch, tmp_path):
    import fcntl

    started = _daemon_start(monkeypatch, tmp_path)
    (tmp_path / "run").mkdir()
    with open(platform_support.daemon_lock_path(), "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        platform_support.start_daemon_if_needed()

    assert started == []

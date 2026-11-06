# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Launch at login on macOS: the daemon's and the menu bar icon's LaunchAgents.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

import arctis_sound_manager.autostart as autostart_mod
from arctis_sound_manager.autostart import (
    LaunchdBackend,
    active_backend_name,
    autostart_enabled,
    set_autostart,
)


@pytest.fixture
def mac(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart_mod, "IS_MACOS", True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python", "asm-daemon", "asm-gui"):
        (bin_dir / name).touch()
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))

    def run(*args, **kwargs):
        raise AssertionError("enabling only writes the agents, it loads nothing")

    monkeypatch.setattr(autostart_mod.subprocess, "run", run)
    return tmp_path


def _agent(home: Path, label: str) -> dict:
    with (home / "Library" / "LaunchAgents" / f"{label}.plist").open("rb") as f:
        return plistlib.load(f)


def test_launchd_is_the_only_backend_on_macos(mac):
    assert active_backend_name() == "launchd"
    assert [b.name for b in autostart_mod._all_backends()] == ["launchd"]


def test_enable_writes_both_agents_with_absolute_paths(mac):
    set_autostart(True)

    daemon = _agent(mac, autostart_mod.LAUNCHD_DAEMON_LABEL)
    assert daemon["ProgramArguments"] == [str(mac / "venv" / "bin" / "asm-daemon")]
    assert daemon["RunAtLoad"] is True
    assert daemon["KeepAlive"] == {"SuccessfulExit": False}
    assert daemon["StandardErrorPath"] == str(mac / "Library" / "Logs" / "asm-daemon.log")

    gui = _agent(mac, autostart_mod.LAUNCHD_GUI_LABEL)
    assert gui["ProgramArguments"] == [str(mac / "venv" / "bin" / "asm-gui"), "--systray"]
    assert "KeepAlive" not in gui
    assert autostart_enabled()


def test_disable_removes_both_agents(mac):
    set_autostart(True)

    set_autostart(False)

    assert not autostart_enabled()
    assert not list((mac / "Library" / "LaunchAgents").iterdir())


def test_a_hand_installed_daemon_agent_reads_as_enabled(mac):
    # docs/macos.md installs the daemon's agent under the same label.
    agents = mac / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / f"{autostart_mod.LAUNCHD_DAEMON_LABEL}.plist").write_text("<plist/>")

    assert LaunchdBackend().is_enabled()


def test_launchd_is_not_offered_on_linux(monkeypatch):
    monkeypatch.setattr(autostart_mod, "IS_MACOS", False)

    assert not LaunchdBackend().is_available()
    assert "launchd" not in [b.name for b in autostart_mod._all_backends()]

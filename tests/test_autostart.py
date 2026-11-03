# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path

import pytest

import arctis_sound_manager.autostart as autostart_mod
from arctis_sound_manager.autostart import (
    HyprlandBackend,
    SwayBackend,
    XdgAutostartBackend,
    SystemdBackend,
    detect_environment,
    pick_backend,
    autostart_enabled,
    set_autostart,
    systemd_user_available,
)


@pytest.fixture(autouse=True)
def _linux_backends(monkeypatch):
    # These are the Linux backends; on macOS only launchd is offered
    # (test_autostart_launchd.py).
    monkeypatch.setattr(autostart_mod, "IS_MACOS", False)


# ── detect_environment ─────────────────────────────────────────────────────────

def test_detect_hyprland_via_env_signature(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc")
    monkeypatch.delenv("SWAYSOCK", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    assert detect_environment() == "hyprland"


def test_detect_sway_via_swaysock(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.setenv("SWAYSOCK", "/run/sway")
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    assert detect_environment() == "sway"


def test_detect_gnome_via_xdg(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("SWAYSOCK", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    assert detect_environment() == "gnome"


def test_detect_unknown_fallback(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("SWAYSOCK", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    assert detect_environment() == "unknown"


# ── pick_backend ───────────────────────────────────────────────────────────────

def test_pick_backend_hyprland_prefers_config_file(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc")
    monkeypatch.delenv("SWAYSOCK", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.setattr(HyprlandBackend, "is_available", lambda self: True)
    backend = pick_backend()
    assert backend.name == "hyprland"


def test_pick_backend_fallback_xdg_when_no_systemd(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("SWAYSOCK", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setattr(autostart_mod, "systemd_user_available", lambda: False)
    backend = pick_backend()
    assert backend.name == "xdg"


# ── XdgAutostartBackend ────────────────────────────────────────────────────────

def test_xdg_enable_creates_desktop_file(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    backend = XdgAutostartBackend()
    backend.enable()
    desktop = tmp_path / ".config" / "autostart" / "arctis-manager.desktop"
    assert desktop.exists()
    content = desktop.read_text()
    assert "[Desktop Entry]" in content
    assert "X-GNOME-Autostart-enabled=true" in content
    assert "--systray" in content


def test_xdg_disable_removes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    backend = XdgAutostartBackend()
    backend.enable()
    assert backend.is_enabled()
    backend.disable()
    assert not backend.is_enabled()
    assert not (tmp_path / ".config" / "autostart" / "arctis-manager.desktop").exists()


def test_xdg_is_enabled_false_when_hidden_true(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    desktop = tmp_path / ".config" / "autostart" / "arctis-manager.desktop"
    desktop.parent.mkdir(parents=True)
    desktop.write_text("[Desktop Entry]\nHidden=true\n")
    backend = XdgAutostartBackend()
    assert not backend.is_enabled()


# ── HyprlandBackend ────────────────────────────────────────────────────────────

def test_hyprland_enable_creates_config(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Pin the resolved exe so the test is deterministic regardless of whether
    # asm-gui is on PATH on the machine running the suite.
    monkeypatch.setattr(autostart_mod.shutil, "which", lambda name: None)
    hypr_dir = tmp_path / ".config" / "hypr"
    hypr_dir.mkdir(parents=True)
    backend = HyprlandBackend()
    backend.enable()
    conf = tmp_path / ".config" / "hypr" / "hyprland.conf"
    assert conf.exists()
    content = conf.read_text()
    assert "# arctis-sound-manager-autostart" in content
    assert "exec-once = asm-gui --systray" in content


def test_hyprland_enable_appends_to_existing_config(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(autostart_mod.shutil, "which", lambda name: None)
    hypr_dir = tmp_path / ".config" / "hypr"
    hypr_dir.mkdir(parents=True)
    conf = hypr_dir / "hyprland.conf"
    conf.write_text("monitor=,preferred,auto,1\n")
    backend = HyprlandBackend()
    backend.enable()
    content = conf.read_text()
    assert "monitor=,preferred,auto,1" in content
    assert "# arctis-sound-manager-autostart" in content
    assert "exec-once = asm-gui --systray" in content


def test_hyprland_disable_removes_only_marker_block(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    hypr_dir = tmp_path / ".config" / "hypr"
    hypr_dir.mkdir(parents=True)
    conf = hypr_dir / "hyprland.conf"
    conf.write_text(
        "monitor=,preferred,auto,1\n"
        "# arctis-sound-manager-autostart\n"
        "exec-once = asm-gui --systray\n"
        "env = XCURSOR_SIZE,24\n"
    )
    backend = HyprlandBackend()
    backend.disable()
    content = conf.read_text()
    assert "monitor=,preferred,auto,1" in content
    assert "env = XCURSOR_SIZE,24" in content
    assert "# arctis-sound-manager-autostart" not in content
    assert "exec-once = asm-gui --systray" not in content


def test_hyprland_enable_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    hypr_dir = tmp_path / ".config" / "hypr"
    hypr_dir.mkdir(parents=True)
    backend = HyprlandBackend()
    backend.enable()
    backend.enable()
    conf = tmp_path / ".config" / "hypr" / "hyprland.conf"
    content = conf.read_text()
    assert content.count("# arctis-sound-manager-autostart") == 1


# ── autostart_enabled / set_autostart ─────────────────────────────────────────

def test_autostart_enabled_returns_true_if_any_backend_active(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(autostart_mod, "systemd_user_available", lambda: False)
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("SWAYSOCK", raising=False)

    xdg_backend = XdgAutostartBackend()
    xdg_backend.enable()
    assert autostart_enabled()


def test_set_autostart_false_purges_all(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(autostart_mod, "systemd_user_available", lambda: False)
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("SWAYSOCK", raising=False)

    xdg_backend = XdgAutostartBackend()
    xdg_backend.enable()
    assert autostart_enabled()

    hypr_dir = tmp_path / ".config" / "hypr"
    hypr_dir.mkdir(parents=True)
    hypr_backend = HyprlandBackend()
    hypr_backend.enable()

    set_autostart(False)
    assert not autostart_enabled()
    assert not xdg_backend.is_enabled()
    assert not hypr_backend.is_enabled()


# ── the tray unit's name, and getting off the old one ──────────────────────────

def test_gui_unit_is_written_under_the_portal_compatible_name(monkeypatch, tmp_path):
    # The name is what gives the process an app id (see service_control's map);
    # written under any other one, the clip shortcut cannot bind.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(autostart_mod.shutil, "which", lambda _: "/usr/bin/asm-gui")
    monkeypatch.setattr(autostart_mod.sc, "daemon_reload", lambda: True)

    written = SystemdBackend()._ensure_gui_service()

    assert written == tmp_path / ".config" / "systemd" / "user" / "app-ArctisManager.service"
    assert "ExecStart=/usr/bin/asm-gui --systray" in written.read_text()


def test_legacy_gui_unit_is_disabled_and_removed(monkeypatch, tmp_path):
    # A left-behind arctis-gui.service starts a second tray at login — the one
    # with the empty app id — and shadows the packaged unit even when disabled.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(autostart_mod.shutil, "which", lambda _: "/usr/bin/asm-gui")
    monkeypatch.setattr(autostart_mod.sc, "daemon_reload", lambda: True)

    legacy = tmp_path / ".config" / "systemd" / "user" / "arctis-gui.service"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[Service]\nExecStart=/usr/bin/asm-gui --systray\n")

    disabled: list[str] = []
    monkeypatch.setattr(autostart_mod.sc, "disable", lambda name: disabled.append(name))

    SystemdBackend()._ensure_gui_service()

    assert disabled == ["arctis-gui.service"]
    assert not legacy.exists()

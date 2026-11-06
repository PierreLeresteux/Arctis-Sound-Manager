# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The Sonar EQ on macOS: the Game curve goes to the daemon's soft EQ, and
nothing reaches for PipeWire.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from arctis_sound_manager.eq_types import EqBand
from arctis_sound_manager.gui import sonar_page


def _on_macos(monkeypatch, tmp_path):
    reloads = []
    monkeypatch.setattr(sonar_page, "AUDIO_SUPPORTED", False)
    monkeypatch.setattr(sonar_page, "IS_MACOS", True)
    monkeypatch.setattr(sonar_page, "_load_smart_volume",
                        lambda: {"enabled": True, "level": 40.0, "loudness": "quiet"})
    monkeypatch.setattr("arctis_sound_manager.sonar_to_pipewire._eq_state_path",
                        lambda channel: tmp_path / f"sonar_eq_state_{channel}.json")
    monkeypatch.setattr("arctis_sound_manager.sonar_to_pipewire._micro_state_path",
                        lambda: tmp_path / "sonar_micro_state.json")
    monkeypatch.setattr(sonar_page, "_load_micro_proc",
                        lambda: {"noiseCanceling": {"enabled": True},
                                 "noiseGate": {"enabled": True, "value": -50.0}})
    monkeypatch.setattr("arctis_sound_manager.gui.dbus_wrapper.DbusWrapper.reload_soft_eq",
                        staticmethod(lambda *a: reloads.append(a)))
    return reloads


def _channel(name):
    widget = sonar_page.SonarChannelWidget.__new__(sonar_page.SonarChannelWidget)
    widget._channel = name
    widget._cur_bands = [EqBand(freq=100, gain=3, q=1)]
    widget._macros = MagicMock(get_values=lambda: (1.0, 0.0, -2.0))
    widget._on_apply_result = MagicMock()
    return widget


def test_the_game_curve_goes_to_the_soft_eq(monkeypatch, tmp_path):
    reloads = _on_macos(monkeypatch, tmp_path)
    monkeypatch.setattr(sonar_page, "_load_boost", lambda: {"enabled": True, "db": 4.0})

    widget = _channel("game")
    widget._do_apply()

    state = json.loads((tmp_path / "sonar_eq_state_game.json").read_text())
    assert state["bands"][0]["gain"] == 3 and state["basses_db"] == 1.0
    assert state["aigus_db"] == -2.0 and state["boost_db"] == 4.0
    assert state["smart_volume"] == {"enabled": True, "level": 40.0, "loudness": "quiet"}
    assert len(reloads) == 1
    widget._on_apply_result.assert_called_once_with(True)


def test_the_micro_curve_and_processing_go_to_the_soft_eq(monkeypatch, tmp_path):
    reloads = _on_macos(monkeypatch, tmp_path)
    monkeypatch.setattr(sonar_page, "_load_boost", lambda: {"enabled": True, "db": 3.0})

    _channel("micro")._do_apply()

    state = json.loads((tmp_path / "sonar_micro_state.json").read_text())
    assert state["bands"][0]["gain"] == 3 and state["boost_db"] == 3.0
    assert state["noise_reduction"]["noiseGate"]["value"] == -50.0
    assert len(reloads) == 1


def test_other_channels_have_nothing_to_run_on(monkeypatch, tmp_path):
    reloads = _on_macos(monkeypatch, tmp_path)

    _channel("chat")._do_apply()

    assert reloads == [] and not (tmp_path / "sonar_eq_state_chat.json").exists()


def test_a_disabled_boost_is_no_boost(monkeypatch, tmp_path):
    _on_macos(monkeypatch, tmp_path)
    monkeypatch.setattr(sonar_page, "_load_boost", lambda: {"enabled": False, "db": 9.0})

    sonar_page._apply_soft_eq([], 0.0, 0.0, 0.0)

    assert json.loads((tmp_path / "sonar_eq_state_game.json").read_text())["boost_db"] == 0.0


def test_spatial_audio_is_not_applied_without_pipewire(monkeypatch):
    from arctis_sound_manager import core

    monkeypatch.setattr(core, "AUDIO_SUPPORTED", False)
    monkeypatch.setattr(core.device_state, "is_device_set", lambda: True)
    engine = core.CoreEngine.__new__(core.CoreEngine)

    assert engine.apply_spatial_audio() is None
    assert engine.regenerate_hesuvi() is False
    assert engine.reset_filter_chain_safe_mode() is False


# ── The soft EQ bar ───────────────────────────────────────────────────────────

@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _bar(monkeypatch, tmp_path, qapp, **config):
    from arctis_sound_manager import soft_eq
    from arctis_sound_manager.gui import soft_eq_bar

    monkeypatch.setattr(soft_eq, "CONFIG_FILE", tmp_path / "soft_eq.json")
    soft_eq.save_config(config)
    monkeypatch.setattr("arctis_sound_manager.coreaudio.devices", lambda side: [])
    reloads = []
    monkeypatch.setattr(soft_eq_bar.DbusWrapper, "reload_soft_eq",
                        staticmethod(lambda *a: reloads.append(a)))
    return soft_eq_bar.SoftEqBar(), reloads


def test_the_7_1_switch_is_saved_and_reloads(monkeypatch, tmp_path, qapp):
    from arctis_sound_manager import soft_eq
    bar, reloads = _bar(monkeypatch, tmp_path, qapp)

    bar._surround.setChecked(True)

    assert soft_eq.load_config()["surround"] is True and reloads


def test_the_status_names_the_blackholes_and_the_layout(monkeypatch, tmp_path, qapp):
    bar, _ = _bar(monkeypatch, tmp_path, qapp, surround=True)

    bar._on_status({"running": True, "output": "Arctis 9 Game", "loopback": "BlackHole 16ch",
                    "layout": "7.1", "mic": {"running": False}})
    assert "BlackHole 16ch" in bar._status.text() and "7.1" in bar._status.text()
    assert "BlackHole 2ch" in bar._mic_status.text()

    bar._on_status({"running": True, "output": "Arctis 9 Game", "loopback": "BlackHole 16ch",
                    "layout": "unset", "mic": {}})
    assert "Configure Speakers" in bar._status.text()

    bar._surround.blockSignals(True)
    bar._surround.setChecked(False)
    bar._on_status({"running": False, "mic": {}})
    assert "BlackHole 2ch" in bar._status.text() and "BlackHole 16ch" in bar._mic_status.text()

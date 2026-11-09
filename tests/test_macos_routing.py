# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""macOS: the Mac's default output follows the headset, and generic mode
plays the software equaliser on a device the user names."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("numpy")

from arctis_sound_manager import coreaudio, device_state, macos_routing  # noqa: E402
from arctis_sound_manager.coreaudio import AudioDevice  # noqa: E402

SPEAKERS = AudioDevice(1, "MacBook Pro Speakers", "BuiltInSpeakerDevice", 2)
BLACKHOLE = AudioDevice(2, "BlackHole 2ch", "BlackHole2ch_UID", 2)
HEADSET = AudioDevice(3, "Arctis 9 Game", "arctis-game", 2)
CHAT = AudioDevice(4, "Arctis 9 Chat", "arctis-chat", 2)
TEAMS = AudioDevice(5, "Microsoft Teams Audio", "MSLoopbackDriverDevice_UID", 2)
DAC = AudioDevice(6, "Schiit Modi", "schiit-uid", 2)
MIC = AudioDevice(7, "MacBook Pro Microphone", "BuiltInMicrophoneDevice", 1)
VIRTUAL = int.from_bytes(b'virt', 'big')
USB = int.from_bytes(b'usb ', 'big')


class FakeCoreAudio:
    def __init__(self, outputs=(TEAMS, SPEAKERS, BLACKHOLE, CHAT, HEADSET, DAC), default=SPEAKERS):
        self.outputs = list(outputs)
        self.inputs = [MIC, BLACKHOLE]
        self.default = default.id
        self.kinds = {SPEAKERS.id: coreaudio.TRANSPORT_BUILT_IN, TEAMS.id: VIRTUAL,
                      BLACKHOLE.id: VIRTUAL}

    def devices(self, side):
        return list(self.outputs if side == 'output' else self.inputs)

    def default_device(self, side):
        return self.default

    def set_default_device(self, side, device_id):
        self.default = device_id

    def transport_type(self, device_id):
        return self.kinds.get(device_id, USB)


# ── the pickers ─────────────────────────────────────────────────────────────

def test_pickers_list_devices_by_uid_without_the_blackholes():
    ca = FakeCoreAudio()
    listed = macos_routing.choices('output', ca=ca)
    assert [d['id'] for d in listed] == ['MSLoopbackDriverDevice_UID', 'BuiltInSpeakerDevice',
                                         'arctis-chat', 'arctis-game', 'schiit-uid']
    assert listed[1] == {'id': 'BuiltInSpeakerDevice', 'name': 'MacBook Pro Speakers',
                         'description': 'MacBook Pro Speakers'}
    assert [d['id'] for d in macos_routing.choices('input', ca=ca)] == ['BuiltInMicrophoneDevice']


def test_a_saved_uid_resolves_to_the_plugged_in_device():
    ca = FakeCoreAudio()
    assert macos_routing.resolve('schiit-uid', ca=ca) == 'Schiit Modi'
    assert macos_routing.resolve('Schiit Modi', ca=ca) == 'Schiit Modi'
    assert macos_routing.resolve('gone-uid', ca=ca) is None
    assert macos_routing.resolve('BlackHole2ch_UID', ca=ca) is None


# ── headset online ──────────────────────────────────────────────────────────

def test_online_moves_the_mac_to_the_headset_game_side_first():
    ca = FakeCoreAudio()
    assert macos_routing.take_default('', '', ca=ca) == 'Arctis 9 Game'
    assert ca.default == HEADSET.id


def test_online_goes_through_the_equaliser_while_it_runs():
    ca = FakeCoreAudio()
    assert macos_routing.take_default('BlackHole 2ch', '', ca=ca) == 'BlackHole 2ch'
    assert ca.default == BLACKHOLE.id


def test_online_uses_the_device_the_user_named():
    ca = FakeCoreAudio()
    macos_routing.take_default('', 'schiit-uid', ca=ca)
    assert ca.default == DAC.id


def test_online_changes_nothing_already_there_or_without_a_headset():
    ca = FakeCoreAudio(default=HEADSET)
    assert macos_routing.take_default('', '', ca=ca) is None
    ca = FakeCoreAudio(outputs=(SPEAKERS, DAC))
    assert macos_routing.take_default('', '', ca=ca) is None
    assert ca.default == SPEAKERS.id


# ── headset offline ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('current', [HEADSET, CHAT, BLACKHOLE])
def test_offline_moves_the_mac_off_the_headset_to_the_chosen_device(current):
    ca = FakeCoreAudio(default=current)
    assert macos_routing.leave_default('schiit-uid', ca=ca) == 'Schiit Modi'
    assert ca.default == DAC.id


def test_offline_falls_back_to_the_built_in_speakers_not_a_virtual_device():
    ca = FakeCoreAudio(default=HEADSET)
    assert macos_routing.leave_default('', ca=ca) == 'MacBook Pro Speakers'
    ca = FakeCoreAudio(default=HEADSET)
    assert macos_routing.leave_default('unplugged-uid', ca=ca) == 'MacBook Pro Speakers'


def test_offline_without_built_in_takes_a_real_device():
    ca = FakeCoreAudio(outputs=(TEAMS, HEADSET, DAC), default=HEADSET)
    assert macos_routing.leave_default('', ca=ca) == 'Schiit Modi'


def test_offline_leaves_an_output_the_user_already_moved():
    ca = FakeCoreAudio(default=DAC)
    assert macos_routing.leave_default('BuiltInSpeakerDevice', ca=ca) is None
    assert ca.default == DAC.id


def test_offline_never_picks_the_headset_itself():
    ca = FakeCoreAudio(default=HEADSET)
    macos_routing.leave_default('arctis-chat', ca=ca)
    assert ca.default == SPEAKERS.id


def test_generic_modes_device_counts_as_the_headset():
    ca = FakeCoreAudio(default=DAC)
    assert macos_routing.leave_default('', also=('Schiit Modi',), ca=ca) == 'MacBook Pro Speakers'


# ── the core ────────────────────────────────────────────────────────────────

def _engine(**settings):
    from arctis_sound_manager.core import CoreEngine

    engine = CoreEngine.__new__(CoreEngine)
    engine.logger = MagicMock()
    engine.general_settings = MagicMock(**{
        'redirect_audio_on_connect': True, 'redirect_audio_on_disconnect': True,
        'redirect_audio_on_disconnect_device': 'schiit-uid', 'generic_device_mode': False,
        **settings})
    engine.is_device_online = lambda: True
    engine.pa_audio_manager = MagicMock()
    return engine


@pytest.fixture
def macos(monkeypatch, tmp_path):
    from arctis_sound_manager import core, soft_eq
    monkeypatch.setattr(core, 'IS_MACOS', True)
    monkeypatch.setattr(core, 'AUDIO_SUPPORTED', False)
    monkeypatch.setattr(soft_eq, 'CONFIG_FILE', tmp_path / 'soft_eq.json')
    ca = FakeCoreAudio()
    monkeypatch.setattr(macos_routing, 'coreaudio', ca)
    for fn in ('devices', 'default_device', 'set_default_device', 'transport_type'):
        monkeypatch.setattr(coreaudio, fn, getattr(ca, fn))
    device_state.clear()
    yield ca
    device_state.clear()


def test_connect_on_macos_moves_the_mac_output_not_pipewire(macos):
    engine = _engine()
    engine.redirect_to_media_sink()
    assert macos.default == HEADSET.id
    engine.pa_audio_manager.redirect_audio.assert_not_called()


def test_connect_follows_the_running_equaliser(macos):
    engine = _engine()
    engine.soft_eq = MagicMock()
    engine.soft_eq.output.status = {'running': True, 'loopback': 'BlackHole 2ch'}
    engine.redirect_to_media_sink()
    assert macos.default == BLACKHOLE.id


def test_connect_uses_play_on(macos):
    from arctis_sound_manager import soft_eq
    soft_eq.CONFIG_FILE.write_text(json.dumps({'output_device': 'Schiit Modi'}))
    _engine().redirect_to_media_sink()
    assert macos.default == DAC.id


def test_connect_respects_the_setting(macos):
    _engine(redirect_audio_on_connect=False).redirect_to_media_sink()
    assert macos.default == SPEAKERS.id


def test_disconnect_on_macos_moves_to_the_chosen_device(macos):
    macos.default = HEADSET.id
    engine = _engine()
    engine.redirect_audio_on_disconnect()
    assert macos.default == DAC.id
    engine.pa_audio_manager.redirect_audio.assert_not_called()


def test_disconnect_respects_the_setting(macos):
    macos.default = HEADSET.id
    _engine(redirect_audio_on_disconnect=False).redirect_audio_on_disconnect()
    assert macos.default == HEADSET.id


# ── generic mode ────────────────────────────────────────────────────────────

def _generic_engine(output='schiit-uid', source=''):
    from arctis_sound_manager.config import load_device_configurations
    from arctis_sound_manager.core import CoreEngine

    engine = CoreEngine.__new__(CoreEngine)
    engine.logger = MagicMock()
    engine._logged_no_device = False
    engine._device_ready = False
    engine.device_config = None
    engine.usb_device = None
    engine.device_status = None
    engine.oled_manager = None
    engine.general_settings = MagicMock(generic_device_mode=True, generic_output_device=output,
                                        generic_input_device=source)
    engine.device_configurations = load_device_configurations()
    engine.setup_loopbacks = MagicMock()
    engine._claim_default_source = MagicMock()
    engine.redirect_to_media_sink = MagicMock()
    engine.teardown = MagicMock()
    return engine


def test_generic_mode_on_macos_takes_the_named_device_without_pipewire(macos):
    engine = _generic_engine(source='BuiltInMicrophoneDevice')
    with patch('arctis_sound_manager.sonar_to_pipewire.check_and_fix_stale_configs') as repair:
        assert engine._setup_generic_device() is True
    assert device_state.get_physical_out_game() == 'Schiit Modi'
    assert device_state.get_physical_in() == 'MacBook Pro Microphone'
    assert engine._device_ready is True
    assert engine.is_device_online() is True
    engine.redirect_to_media_sink.assert_called_once()
    repair.assert_not_called()
    engine.setup_loopbacks.assert_not_called()
    engine._claim_default_source.assert_not_called()


def test_generic_mode_waits_for_a_device_that_is_not_plugged_in(macos):
    engine = _generic_engine(output='gone-uid')
    assert engine._setup_generic_device() is True
    assert engine._device_ready is False
    assert device_state.get_physical_out_game() == ''


def test_the_equaliser_plays_on_generic_modes_device():
    from arctis_sound_manager import soft_eq

    engine = soft_eq.SoftEqEngine(ca=FakeCoreAudio(), fallback_output=lambda: 'Schiit Modi',
                                  fallback_mic=lambda: 'MacBook Pro Microphone')
    devices = {'output': engine._ca.devices('output'), 'input': engine._ca.devices('input')}
    config = soft_eq.load_config() | {'output_device': '', 'mic_input_device': ''}
    assert engine.output.physical(config, devices)[0] == DAC
    assert engine.mic.physical(config, devices)[0] == MIC
    # "Play on" still wins.
    config['output_device'] = 'arctis-game'
    assert engine.output.physical(config, devices)[0] == HEADSET


def test_settings_list_coreaudio_devices_on_macos(monkeypatch):
    from arctis_sound_manager import dbus_service
    from arctis_sound_manager.dbus_service import ArctisManagerDbusSettingsService

    monkeypatch.setattr(dbus_service, 'IS_MACOS', True)
    monkeypatch.setattr(macos_routing, 'choices',
                        lambda side: [{'id': side, 'name': side, 'description': side}])
    get = ArctisManagerDbusSettingsService.get_list_options.__dict__["__DBUS_METHOD"].fn
    service = MagicMock()
    for name, side in (('pulse_audio_devices', 'output'), ('external_audio_devices', 'output'),
                       ('pulse_audio_sources', 'input')):
        assert json.loads(get(service, name))[0]['id'] == side
    service.core_engine.pa_audio_manager.sink_list_wrapper.assert_not_called()


def test_turning_generic_mode_off_on_macos_drops_its_device(macos):
    engine = _generic_engine()
    engine._setup_generic_device()
    assert engine.device_config.generic
    engine.general_settings.generic_device_mode = False
    engine.general_settings.preferred_device = None
    engine._detect_lock = threading.Lock()
    engine._device_configured_event = threading.Event()
    engine._find_hid_device = MagicMock(return_value=None)
    engine._libusb_list_is_stale = MagicMock(return_value=False)
    engine.configure_virtual_sinks()
    engine.teardown.assert_called_once()

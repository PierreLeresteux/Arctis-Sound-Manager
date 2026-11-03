# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The macOS software equaliser's engine: when it runs, and what it does to
the system's default output. Fake CoreAudio and sounddevice stand in for
the Mac.
"""

from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")

from arctis_sound_manager import soft_eq  # noqa: E402
from arctis_sound_manager.coreaudio import AudioDevice  # noqa: E402
from arctis_sound_manager.eq_types import EqBand  # noqa: E402
from arctis_sound_manager.platform_support import IS_MACOS  # noqa: E402

SPEAKERS = AudioDevice(1, "MacBook Pro Speakers", "BuiltInSpeakerDevice", 2)
BLACKHOLE = AudioDevice(2, "BlackHole 2ch", "BlackHole2ch_UID", 2)
HEADSET = AudioDevice(3, "Arctis 9 Game", "arctis-game", 2)
CHAT = AudioDevice(4, "Arctis 9 Chat", "arctis-chat", 2)
BLACKHOLE16 = AudioDevice(5, "BlackHole 16ch", "BlackHole16ch_UID", 16)
BUILTIN_MIC = AudioDevice(6, "MacBook Pro Microphone", "BuiltInMicrophoneDevice", 1)
HEADSET_MIC = AudioDevice(7, "Arctis 9 Chat", "arctis-chat", 1)
#: Audio MIDI Setup's 7.1 Surround on a 16-channel device: L R C LFE Ls Rs
#: Rls Rrs, the rest discrete.
SEVEN_ONE = [1, 2, 3, 4, 5, 6, 33, 34] + [(1 << 16) | i for i in range(8, 16)]


class FakeCoreAudio:
    def __init__(self):
        self.outputs = [SPEAKERS, BLACKHOLE, CHAT, HEADSET, BLACKHOLE16]
        self.inputs = [BUILTIN_MIC, BLACKHOLE, HEADSET_MIC, BLACKHOLE16]
        self.default = {"output": SPEAKERS.id, "input": BUILTIN_MIC.id}
        self.layouts = {BLACKHOLE16.id: SEVEN_ONE}

    def channel_layout(self, device_id):
        return list(self.layouts.get(device_id, []))

    def devices(self, side):
        return list(self.outputs if side == "output" else self.inputs)

    def default_device(self, side):
        return self.default[side]

    def set_default_device(self, side, device_id):
        self.default[side] = device_id


class FakeStream:
    def __init__(self, device, callback, channels, **kwargs):
        self.device = device
        self.callback = callback
        self.channels = channels
        self.active = False
        self.closed = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True


class FakeSoundDevice:
    """PortAudio's one list: each device once, with both channel counts."""

    def __init__(self, ca):
        self.ca = ca
        self.streams = []
        self.refreshes = 0
        self.listed = self._list()

    def _list(self):
        names = []
        for d in self.ca.outputs + self.ca.inputs:
            if d.name not in names:
                names.append(d.name)
        def count(devices, name):
            return max([d.channels for d in devices if d.name == name] or [0])
        return [{"name": n, "max_input_channels": count(self.ca.inputs, n),
                 "max_output_channels": count(self.ca.outputs, n)} for n in names]

    def _terminate(self):
        pass

    def _initialize(self):
        self.refreshes += 1
        self.listed = self._list()

    def query_devices(self):
        return self.listed

    def index(self, name):
        return [d["name"] for d in self.listed].index(name)

    def Stream(self, device, callback, channels, **kwargs):
        stream = FakeStream(device, callback, channels, **kwargs)
        self.streams.append(stream)
        return stream


@pytest.fixture
def mac(monkeypatch, tmp_path):
    for name, file in (("CONFIG_FILE", "soft_eq.json"), ("RESTORE_FILE", "soft_eq_restore.json"),
                       ("MIC_RESTORE_FILE", "soft_eq_mic_restore.json"),
                       ("EQ_MODE_FILE", ".eq_mode"), ("SPATIAL_FILE", "sonar_spatial_audio.json")):
        monkeypatch.setattr(soft_eq, name, tmp_path / file)
    monkeypatch.setattr("arctis_sound_manager.sonar_to_pipewire._eq_state_path",
                        lambda channel: tmp_path / f"sonar_eq_state_{channel}.json")
    monkeypatch.setattr("arctis_sound_manager.sonar_to_pipewire._micro_state_path",
                        lambda: tmp_path / "sonar_micro_state.json")
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path))
    (tmp_path / "sonar_spatial_audio.json").write_text(json.dumps({"enabled": False}))
    ca = FakeCoreAudio()
    sd = FakeSoundDevice(ca)
    engine = soft_eq.SoftEqEngine(sounddevice=sd, ca=ca)
    (tmp_path / ".eq_mode").write_text("sonar")
    soft_eq.save_config({"enabled": True, "output_device": ""})
    return engine, ca, sd, tmp_path


def test_turned_on_it_plays_blackhole_on_the_headset_game_output(mac):
    engine, ca, sd, _ = mac

    engine.tick()

    assert engine.status["running"] and engine.status["output"] == "Arctis 9 Game"
    assert sd.streams[0].device == (sd.index("BlackHole 2ch"), sd.index("Arctis 9 Game"))
    assert ca.default["output"] == BLACKHOLE.id
    assert ca.default["input"] == BUILTIN_MIC.id          # the microphone route is off


def test_off_or_custom_mode_gives_the_output_back(mac):
    engine, ca, sd, tmp_path = mac
    engine.tick()

    (tmp_path / ".eq_mode").write_text("custom")
    engine.tick()

    assert not engine.status["running"] and sd.streams[0].closed
    assert ca.default["output"] == SPEAKERS.id


def test_headset_unplugged_restores_the_output(mac):
    engine, ca, _sd, _ = mac
    engine.tick()

    ca.outputs = [SPEAKERS, BLACKHOLE]
    engine.tick()

    assert ca.default["output"] == SPEAKERS.id
    assert engine.status["error"] == "Headset output not found"


def test_without_blackhole_nothing_is_touched(mac):
    engine, ca, sd, _ = mac
    ca.outputs = [SPEAKERS, HEADSET]

    engine.tick()

    assert not sd.streams and ca.default["output"] == SPEAKERS.id
    assert "brew install blackhole-2ch" in engine.status["error"]


def test_a_chosen_output_wins_over_the_headset(mac):
    engine, _ca, sd, _ = mac
    soft_eq.save_config({"enabled": True, "output_device": "MacBook Pro Speakers"})

    engine.tick()

    assert sd.streams[0].device == (sd.index("BlackHole 2ch"), sd.index("MacBook Pro Speakers"))


def test_a_daemon_that_died_routing_still_gives_the_output_back(mac):
    engine, ca, _sd, _ = mac
    engine.tick()
    fresh = soft_eq.SoftEqEngine(sounddevice=FakeSoundDevice(ca), ca=ca)

    soft_eq.save_config({"enabled": False, "output_device": ""})
    fresh.tick()

    assert ca.default["output"] == SPEAKERS.id


def test_a_user_choice_of_output_is_not_fought(mac):
    engine, ca, _sd, _ = mac
    engine.tick()

    ca.default["output"] = CHAT.id
    engine.tick()

    assert ca.default["output"] == CHAT.id


def test_reload_changes_the_curve_without_a_new_stream(mac):
    engine, _ca, sd, tmp_path = mac
    engine.tick()
    (tmp_path / "sonar_eq_state_game.json").write_text(json.dumps({
        "bands": [{"freq": 1000, "gain": 6, "q": 1, "type": "peakingEQ", "enabled": True}],
        "basses_db": 0, "voix_db": 0, "aigus_db": 0, "boost_db": 0}))

    engine.reload()

    cascade, gain, compressor, _spatial = engine._chain
    assert len(sd.streams) == 1
    assert cascade.sections == 4 and gain == pytest.approx(10 ** (-6 / 20))
    assert compressor is None


def test_smart_volume_comes_with_the_curve_and_keeps_its_level(mac):
    engine, _ca, _sd, tmp_path = mac
    state = {"bands": [], "basses_db": 0, "voix_db": 0, "aigus_db": 0, "boost_db": 0,
             "smart_volume": {"enabled": True, "level": 100, "loudness": "loud"}}
    (tmp_path / "sonar_eq_state_game.json").write_text(json.dumps(state))
    engine.reload()
    first = engine._chain[2]
    first.envelope = 0.3

    state["smart_volume"]["level"] = 50
    (tmp_path / "sonar_eq_state_game.json").write_text(json.dumps(state))
    engine.reload()

    second = engine._chain[2]
    assert second is not first and second.envelope == 0.3
    assert second.ratio == pytest.approx(2.0) and second.makeup == pytest.approx(6.0)


@pytest.mark.skipif(not IS_MACOS, reason="vDSP is Accelerate's, macOS only")
def test_the_callback_filters_and_scales(mac):
    engine, _ca, _sd, tmp_path = mac
    soft_eq_state = tmp_path / "sonar_eq_state_game.json"
    soft_eq_state.write_text(json.dumps({"bands": [], "basses_db": 0, "voix_db": 0,
                                         "aigus_db": 0, "boost_db": 6}))
    engine.reload()
    indata = np.full((256, 2), 0.25, dtype=np.float32)
    outdata = np.zeros_like(indata)

    engine.output.callback(indata, outdata, 256, None, None)

    # DC through three 0 dB peaking filters, then +6 dB of boost.
    assert outdata[-1, 0] == pytest.approx(0.25 * 10 ** (6 / 20), rel=1e-3)


def test_pick_output_prefers_the_game_side():
    assert soft_eq.pick_output([SPEAKERS, CHAT, HEADSET], "") == HEADSET
    assert soft_eq.pick_output([SPEAKERS], "") is None
    assert soft_eq.pick_output([SPEAKERS, BLACKHOLE], "BlackHole 2ch") is None


# ── Spatial Audio ─────────────────────────────────────────────────────────────

def _spatial_on(tmp_path, **state):
    cfg = tmp_path / ".config" / "arctis_manager"
    cfg.mkdir(parents=True, exist_ok=True)
    data = {"enabled": True, "auto": False, "mode": "headphones", "immersion": 25,
            "channel_gains": {"FL": -3.0}, **state}
    for path in (tmp_path / "sonar_spatial_audio.json", cfg / "sonar_spatial_audio.json"):
        path.write_text(json.dumps(data))


def test_spatial_off_is_no_settings(mac):
    _engine, _ca, _sd, tmp_path = mac
    _spatial_on(tmp_path, enabled=False)
    assert soft_eq.spatial_settings() is None


@pytest.mark.parametrize("surround", [False, True])
def test_speakers_mode_is_the_downmix_with_the_levels(mac, monkeypatch, surround):
    engine, _ca, _sd, tmp_path = mac
    monkeypatch.setattr("arctis_sound_manager.device_state.get_spatial_engine", lambda: "hesuvi")
    soft_eq.save_config({"surround": surround})
    _spatial_on(tmp_path, mode="speakers")

    engine.reload()

    renderer = engine._chain[3]
    assert renderer.spatializer is None
    assert renderer.downmix[0, 0] == pytest.approx(10 ** (-3 / 20))      # FL at -3 dB


def test_surround_only_never_spatialises_a_stereo_route(mac, monkeypatch):
    engine, _ca, _sd, tmp_path = mac
    monkeypatch.setattr("arctis_sound_manager.device_state.get_spatial_engine", lambda: "hesuvi")
    _spatial_on(tmp_path, auto=True)
    engine.reload()
    assert engine._chain[3].spatializer is None

    soft_eq.save_config({"surround": True})
    engine.reload()
    renderer = engine._chain[3]
    assert renderer.auto and renderer.spatializer.speakers == soft_eq.SPEAKERS


def test_spatial_reads_immersion_and_the_front_levels(mac, monkeypatch):
    _engine, _ca, _sd, tmp_path = mac
    monkeypatch.setattr("arctis_sound_manager.device_state.get_spatial_engine", lambda: "hesuvi")
    _spatial_on(tmp_path)
    settings = soft_eq.spatial_settings()
    assert settings["immersion"] == 25 and settings["gains"]["FL"] == -3.0
    assert settings["distance"] == 50                                # the default


def test_a_headset_without_spatial_engine_gets_none(mac, monkeypatch):
    _engine, _ca, _sd, tmp_path = mac
    monkeypatch.setattr("arctis_sound_manager.device_state.get_spatial_engine", lambda: "none")
    _spatial_on(tmp_path)
    assert soft_eq.spatial_settings() is None


def test_the_spatial_stage_survives_a_curve_edit_and_follows_the_hrir(mac, monkeypatch):
    engine, _ca, _sd, tmp_path = mac
    monkeypatch.setattr("arctis_sound_manager.device_state.get_spatial_engine", lambda: "hesuvi")
    _spatial_on(tmp_path)
    hrir = {"id": "cmss_game"}
    engine._hrir_id = lambda: hrir["id"]

    engine.reload()
    first = engine._chain[3].spatializer
    assert first.wet == pytest.approx(0.5)
    _spatial_on(tmp_path, distance=10)
    engine.reload()
    assert engine._chain[3].spatializer is first and first.wet == pytest.approx(0.1)

    hrir["id"] = "dtshx"
    engine.reload()
    assert engine._chain[3].spatializer is not first and engine._hrir_path.stem == "dtshx"

    soft_eq.save_config({"surround": True})
    engine.reload()
    assert engine._chain[3].spatializer.speakers == soft_eq.SPEAKERS


# ── Microphone ────────────────────────────────────────────────────────────────

def test_the_microphone_route_plays_the_headset_mic_into_blackhole_16ch(mac):
    engine, ca, sd, _ = mac
    soft_eq.save_config({"mic_enabled": True})

    engine.tick()

    mic_stream = next(s for s in sd.streams if s.channels == (1, 2))
    assert mic_stream.device == (sd.index("Arctis 9 Chat"), sd.index("BlackHole 16ch"))
    assert ca.default["input"] == BLACKHOLE16.id
    assert engine.status["mic"]["running"] and engine.status["running"]


def test_turning_the_microphone_off_gives_the_input_back(mac):
    engine, ca, _sd, _ = mac
    soft_eq.save_config({"mic_enabled": True})
    engine.tick()

    soft_eq.save_config({"mic_enabled": False})
    engine.tick()

    assert ca.default["input"] == BUILTIN_MIC.id
    assert engine.status["running"]                      # the output route is not touched


def test_the_microphone_needs_its_own_blackhole(mac):
    engine, ca, _sd, _ = mac
    ca.outputs = [d for d in ca.outputs if d is not BLACKHOLE16]
    ca.inputs = [d for d in ca.inputs if d is not BLACKHOLE16]
    soft_eq.save_config({"mic_enabled": True})

    engine.tick()

    assert "brew install blackhole-16ch" in engine.status["mic"]["error"]
    assert ca.default["input"] == BUILTIN_MIC.id


def test_a_headset_plugged_in_later_refreshes_portaudio_and_reopens_both(mac):
    engine, ca, sd, _ = mac
    soft_eq.save_config({"mic_enabled": True})
    ca.outputs = [SPEAKERS, BLACKHOLE, BLACKHOLE16]
    ca.inputs = [BUILTIN_MIC, BLACKHOLE, BLACKHOLE16]
    sd.listed = sd._list()
    engine.tick()                                        # no headset: nothing runs
    assert not sd.streams

    ca.outputs = [SPEAKERS, BLACKHOLE, CHAT, HEADSET, BLACKHOLE16]
    ca.inputs = [BUILTIN_MIC, BLACKHOLE, HEADSET_MIC, BLACKHOLE16]
    engine.tick()

    assert sd.refreshes == 1
    assert engine.status["running"] and engine.status["mic"]["running"]


def _micro_state(tmp_path, **processing):
    (tmp_path / "sonar_micro_state.json").write_text(json.dumps({
        "bands": [], "basses_db": 0, "voix_db": 0, "aigus_db": 0, "boost_db": 0,
        "noise_canceling": {}, "noise_reduction": processing}))


def test_the_micro_chain_follows_its_saved_state(mac):
    engine, _ca, _sd, tmp_path = mac
    _micro_state(tmp_path, bgReduction={"enabled": True, "value": 0.5},
                 noiseGate={"enabled": True, "value": -45.0},
                 compressor={"enabled": True, "value": 0.5})

    engine.reload()

    cascade, gain, gate, compressor = engine._mic_chain
    assert cascade.sections == 1 and gain == 1.0
    assert gate.threshold == pytest.approx(10 ** (-45 / 20))
    assert compressor.ratio == pytest.approx(5.0)


@pytest.mark.skipif(not IS_MACOS, reason="vDSP is Accelerate's, macOS only")
def test_the_mic_callback_writes_the_voice_on_both_channels(mac):
    engine, _ca, _sd, tmp_path = mac
    _micro_state(tmp_path)
    engine.reload()
    indata = np.full((256, 1), 0.2, dtype=np.float32)
    outdata = np.zeros((256, 2), dtype=np.float32)

    engine.mic.callback(indata, outdata, 256, None, None)

    assert (outdata == 0.2).all()


# ── 7.1 sources ───────────────────────────────────────────────────────────────

def test_the_speaker_map_reads_7_1_and_5_1_as_linux_does():
    matrix, name = soft_eq.speaker_map(SEVEN_ONE)
    assert name == "7.1" and matrix.shape == (8, 8)
    # L R C LFE Ls Rs Rls Rrs -> FL FR FC LFE SL SR RL RR
    assert [soft_eq.SPEAKERS[row.argmax()] for row in matrix] == [
        "FL", "FR", "FC", "LFE", "SL", "SR", "RL", "RR"]

    matrix, name = soft_eq.speaker_map([1, 2, 3, 4, 5, 6])
    assert name == "5.1"
    assert [soft_eq.SPEAKERS[row.argmax()] for row in matrix] == [
        "FL", "FR", "FC", "LFE", "RL", "RR"]


def test_a_loopback_never_set_up_is_stereo():
    matrix, name = soft_eq.speaker_map([0xFFFFFFFF] * 16)
    assert name == "" and matrix.shape == (2, 8)
    assert matrix[0, 0] == matrix[1, 1] == 1.0


def test_7_1_sources_play_blackhole_16ch_and_move_the_mic_to_2ch(mac):
    engine, ca, sd, _ = mac
    soft_eq.save_config({"surround": True, "mic_enabled": True})

    engine.tick()

    out_stream = next(s for s in sd.streams if s.channels == (8, 2))
    assert out_stream.device == (sd.index("BlackHole 16ch"), sd.index("Arctis 9 Game"))
    mic_stream = next(s for s in sd.streams if s.channels == (1, 2))
    assert mic_stream.device == (sd.index("Arctis 9 Chat"), sd.index("BlackHole 2ch"))
    assert ca.default == {"output": BLACKHOLE16.id, "input": BLACKHOLE.id}
    status = engine.status
    assert status["layout"] == "7.1" and status["loopback"] == "BlackHole 16ch"
    assert status["mic"]["loopback"] == "BlackHole 2ch"


def test_switching_7_1_keeps_what_to_give_back(mac):
    engine, ca, _sd, _ = mac
    soft_eq.save_config({"mic_enabled": True})
    engine.tick()

    soft_eq.save_config({"surround": True})
    engine.tick()
    assert ca.default == {"output": BLACKHOLE16.id, "input": BLACKHOLE.id}

    soft_eq.save_config({"enabled": False, "mic_enabled": False})
    engine.tick()
    assert ca.default == {"output": SPEAKERS.id, "input": BUILTIN_MIC.id}


def test_7_1_needs_blackhole_16ch(mac):
    engine, ca, _sd, _ = mac
    ca.outputs = [d for d in ca.outputs if d is not BLACKHOLE16]
    ca.inputs = [d for d in ca.inputs if d is not BLACKHOLE16]
    soft_eq.save_config({"surround": True})

    engine.tick()

    assert "brew install blackhole-16ch" in engine.status["error"]
    assert ca.default["output"] == SPEAKERS.id


def test_an_unset_layout_runs_as_stereo_and_says_so(mac):
    engine, ca, sd, _ = mac
    ca.layouts = {}
    soft_eq.save_config({"surround": True})

    engine.tick()

    assert sd.streams[0].channels == (2, 2) and engine.status["layout"] == "unset"


@pytest.mark.skipif(not IS_MACOS, reason="vDSP is Accelerate's, macOS only")
def test_the_7_1_callback_folds_the_speakers_to_stereo(mac):
    engine, _ca, _sd, _ = mac
    soft_eq.save_config({"surround": True})
    engine.reload()
    indata = np.zeros((256, 8), dtype=np.float32)
    indata[:, 2] = 0.2                                     # the centre
    outdata = np.zeros((256, 2), dtype=np.float32)

    engine.output.callback(indata, outdata, 256, None, None)

    assert outdata[-1].tolist() == pytest.approx([0.2 * 0.7071] * 2, rel=1e-3)


def test_a_callback_between_two_stream_shapes_plays_silence(mac):
    engine, _ca, _sd, _ = mac
    soft_eq.save_config({"surround": True})
    engine.reload()                                        # an 8-speaker chain...
    engine.output._mix = None                              # ...on the stereo stream
    outdata = np.ones((256, 2), dtype=np.float32)

    engine.output.callback(np.full((256, 2), 0.2, dtype=np.float32), outdata, 256, None, None)

    assert not outdata.any()

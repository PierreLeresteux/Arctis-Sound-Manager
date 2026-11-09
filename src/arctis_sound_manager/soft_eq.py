# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Sonar EQ on macOS, run by the daemon on the system's sound and mic.

macOS has no PipeWire to put a filter-chain between the apps and the
headset, and an app cannot filter the system mix without a driver. So the
engine goes through BlackHole, a free loopback driver, both ways:

- **Output.** The system output moves to BlackHole 2ch (`brew install
  blackhole-2ch`): everything the Mac plays lands there, the engine reads
  it, runs the Game channel's Sonar EQ, Smart Volume and Spatial Audio
  (soft_eq_dsp) and plays the result on the headset. On when turned on
  (soft_eq.json) and the equaliser is in Sonar mode (.eq_mode; in "custom"
  mode the headset's own EQ does the job).
- **Microphone.** The engine reads the headset's microphone, runs the
  Micro channel's chain and writes it to BlackHole 16ch (`brew install
  blackhole-16ch`), which becomes the system input: apps hear the
  processed voice. A second device, because whatever is written to a
  BlackHole comes out of it, and the 2ch one carries the Mac's sound.
- **7.1 sources** (soft_eq.json's surround). The two swap: the Mac plays
  to BlackHole 16ch, whose first channels carry the speakers set for it
  in Audio MIDI Setup (Configure Speakers), and the microphone goes
  through BlackHole 2ch. Spatial Audio then places every speaker, as
  HeSuVi does on Linux, and the downmix folds them to stereo otherwise.

Whenever a route cannot run, for whatever reason, the system device it
took goes back to where it was: left on BlackHole with nobody behind it,
the Mac would play to nothing, or record nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path

import numpy as np

from arctis_sound_manager import coreaudio
from arctis_sound_manager.soft_eq_dsp import (SAMPLE_RATE, SPEAKERS, STEREO, Compressor, Gate,
                                              Renderer, Spatializer, StereoBiquads, build_chain,
                                              build_mic_chain, downmix_matrix, hesuvi_impulses,
                                              load_wav, smart_volume_params)

logger = logging.getLogger('SoftEq')

_CFG = Path.home() / '.config' / 'arctis_manager'
CONFIG_FILE = _CFG / 'soft_eq.json'
#: Where the system output / input was before the engine took it, by UID:
#: kept on disk so a daemon that died while routing can still put it back.
RESTORE_FILE = _CFG / 'soft_eq_restore.json'
MIC_RESTORE_FILE = _CFG / 'soft_eq_mic_restore.json'
EQ_MODE_FILE = _CFG / '.eq_mode'
SPATIAL_FILE = _CFG / 'sonar_spatial_audio.json'
DEFAULT_HRIR_ID = 'atmos'
LOOPBACK_NAME = 'BlackHole 2ch'
MIC_LOOPBACK_NAME = 'BlackHole 16ch'
LOOPBACKS = (LOOPBACK_NAME, MIC_LOOPBACK_NAME)
_INSTALL = {LOOPBACK_NAME: 'brew install blackhole-2ch',
            MIC_LOOPBACK_NAME: 'brew install blackhole-16ch'}
CHANNEL = 'game'
BLOCK_FRAMES = 256

_HEADSET_WORDS = ('arctis', 'steelseries')


def load_config() -> dict:
    try:
        raw = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {'enabled': bool(raw.get('enabled', False)),
            'output_device': str(raw.get('output_device') or ''),
            'mic_enabled': bool(raw.get('mic_enabled', False)),
            'mic_input_device': str(raw.get('mic_input_device') or ''),
            'surround': bool(raw.get('surround', False))}


def loopbacks(config: dict) -> tuple[str, str]:
    """(the output's BlackHole, the microphone's): 7.1 sources need the
    multichannel one for the Mac's sound, so the two swap."""
    if config.get('surround'):
        return MIC_LOOPBACK_NAME, LOOPBACK_NAME
    return LOOPBACK_NAME, MIC_LOOPBACK_NAME


#: CoreAudio channel labels -> the source speakers they feed. Left / right
#: centre fold into the front pair, a centre surround into both rears.
_LABELS = {1: {'FL': 1.0}, 2: {'FR': 1.0}, 3: {'FC': 1.0}, 4: {'LFE': 1.0}, 37: {'LFE': 1.0},
           7: {'FL': 1.0}, 8: {'FR': 1.0}, 9: {'RL': 0.7071, 'RR': 0.7071},
           10: {'SL': 1.0}, 11: {'SR': 1.0}, 33: {'RL': 1.0}, 34: {'RR': 1.0}}
_LFE_LABELS = (4, 37)
_REAR_LABELS = (9, 33, 34)


def speaker_map(labels: list[int]) -> tuple[np.ndarray, str]:
    """(channels, 8) matrix from a loopback's channels to SPEAKERS, and the
    layout's name ('7.1', '5.1', ...), from its CoreAudio channel labels.

    Left / Right Surround are the sides when the layout has rears too, and
    the rears otherwise (5.1), as Linux maps them. A layout without a left
    and right (a BlackHole never set up in Audio MIDI Setup) is read as
    stereo on its first two channels, named ''.
    """
    surround = 'SL' if any(label in _REAR_LABELS for label in labels) else 'RL'
    labels = labels[:16]
    targets = []
    for label in labels:
        if label == 5:
            targets.append({surround: 1.0})
        elif label == 6:
            targets.append({'SR' if surround == 'SL' else 'RR': 1.0})
        else:
            targets.append(_LABELS.get(label, {}))
    if not (1 in labels and 2 in labels):
        matrix = np.zeros((2, len(SPEAKERS)), dtype=np.float32)
        matrix[0, 0] = matrix[1, 1] = 1.0
        return matrix, ''
    used = max(i for i, t in enumerate(targets) if t) + 1
    matrix = np.zeros((max(2, used), len(SPEAKERS)), dtype=np.float32)
    for channel, target in enumerate(targets[:used]):
        for speaker, weight in target.items():
            matrix[channel, SPEAKERS.index(speaker)] += weight
    mains = sum(1 for label, t in zip(labels, targets) if t and label not in _LFE_LABELS)
    lfe = sum(1 for label, t in zip(labels, targets) if t and label in _LFE_LABELS)
    return matrix, f'{mains}.{lfe}' 


def save_config(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({**load_config(), **config}))


def eq_mode() -> str:
    try:
        return EQ_MODE_FILE.read_text().strip() or 'custom'
    except OSError:
        return 'custom'


def spatial_settings() -> dict | None:
    """The Game channel's Spatial Audio: the Sonar page's switch, mode
    ('headphones', or 'speakers': no HRTF, the downmix with the speaker
    levels), "surround sources only" (auto), Immersion, Distance and the
    speaker levels. None when off, or for a headset whose profile has no
    spatial engine.
    """
    from arctis_sound_manager import device_state
    from arctis_sound_manager.sonar_to_pipewire import _load_channel_gains, _load_spatial_pct
    try:
        raw = json.loads(SPATIAL_FILE.read_text())
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if not raw.get('enabled', True) or device_state.get_spatial_engine() == 'none':
        return None
    immersion, distance = _load_spatial_pct(CHANNEL)
    return {'mode': 'speakers' if raw.get('mode') == 'speakers' else 'headphones',
            'auto': bool(raw.get('auto', False)), 'immersion': immersion,
            'distance': distance, 'gains': _load_channel_gains(CHANNEL)}


def _pick_headset(devices: list[coreaudio.AudioDevice], wanted: str,
                  prefer: str) -> coreaudio.AudioDevice | None:
    """The device the user chose (by name or UID), else the headset's,
    *prefer* side first.

    The headset is a name with Arctis or SteelSeries in it; models with a
    game and a chat device list both.
    """
    candidates = [d for d in devices if d.name not in LOOPBACKS]
    if wanted:
        return next((d for d in candidates if wanted in (d.name, d.uid)), None)
    headsets = [d for d in candidates if any(w in d.name.lower() for w in _HEADSET_WORDS)]
    headsets.sort(key=lambda d: prefer not in d.name.lower())
    return headsets[0] if headsets else None


def pick_output(devices: list[coreaudio.AudioDevice], wanted: str) -> coreaudio.AudioDevice | None:
    return _pick_headset(devices, wanted, 'game')


def pick_mic(devices: list[coreaudio.AudioDevice], wanted: str) -> coreaudio.AudioDevice | None:
    return _pick_headset(devices, wanted, 'chat')


class _Route:
    """One stream between a BlackHole and the headset, and the system
    default it takes while it runs (the output or the input)."""

    side = 'output'                 # the system default this route takes
    label = 'Soft EQ'

    @property
    def restore_file(self) -> Path:
        return RESTORE_FILE

    def __init__(self, engine: 'SoftEqEngine'):
        self.engine = engine
        self._stream = None
        self._route: tuple | None = None
        self.status = {'running': False, 'enabled': False, 'input': '', 'output': '', 'error': '',
                       'loopback': ''}

    # hooks
    def wanted(self, config: dict) -> bool:
        raise NotImplementedError

    def loopback_name(self, config: dict) -> str:
        raise NotImplementedError

    def physical(self, config: dict, devices: dict) -> tuple[coreaudio.AudioDevice | None, str]:
        raise NotImplementedError

    def ends(self, loopback: coreaudio.AudioDevice,
             physical: str) -> tuple[tuple[str, int], tuple[str, int]]:
        """((input name, channels), (output name, channels)) of the stream."""
        raise NotImplementedError

    def callback(self, indata, outdata, frames, time, status) -> None:
        raise NotImplementedError

    # the decision
    def tick(self, config: dict, devices: dict) -> None:
        self.status['enabled'] = self.wanted(config)
        name = self.status['loopback'] = self.loopback_name(config)
        if not self.status['enabled']:
            self.stop(error='')
            return
        loopback = next((d for d in devices[self.side] if d.name == name), None)
        if loopback is None:
            self.stop(error=f'{name} is not installed: {_INSTALL[name]}')
            return
        physical, error = self.physical(config, devices)
        if physical is None:
            self.stop(error=error)
            return
        (in_name, in_channels), (out_name, out_channels) = self.ends(loopback, physical.name)
        route = (in_name, out_name, in_channels, out_channels)
        if self._stream is not None and (self._route != route or not self._stream.active):
            self.close()
        if self._stream is None:
            if not self._open(route):
                self._restore_default(devices[self.side])
                return
            # Only when the stream starts: a user who then picks another
            # device in the menu bar is not fought every tick.
            self._take_default(loopback, devices[self.side])
        self.status.update(running=True, input=in_name, output=out_name, error='')

    def stop(self, error: str) -> None:
        was_running = self._stream is not None
        self.close()
        self.status.update(running=False, input='', output='', error=error)
        if not was_running and not self.restore_file.exists():
            return                  # never took the device: nothing to give back
        try:
            self._restore_default(self.engine._ca.devices(self.side))
        except OSError as exc:
            logger.warning('could not restore the system %s: %r', self.side, exc)

    # the stream
    def _open(self, route: tuple) -> bool:
        in_channels, out_channels = route[2:]
        sd = self.engine._sounddevice()
        try:
            devices = (self.engine._device_index(route[0], 'input', in_channels),
                       self.engine._device_index(route[1], 'output', out_channels))
            stream = sd.Stream(device=devices, samplerate=SAMPLE_RATE, blocksize=BLOCK_FRAMES,
                               channels=(in_channels, out_channels), dtype='float32',
                               latency='low', callback=self.callback)
            stream.start()
        except Exception as exc:
            self.status.update(running=False, error=f'Audio stream: {exc}')
            logger.warning('could not open %s -> %s: %r', route[0], route[1], exc)
            return False
        self._stream, self._route = stream, route
        logger.info('%s running: %s (%d) -> %s (%d)', self.label, route[0], in_channels,
                    route[1], out_channels)
        return True

    def close(self) -> None:
        stream, self._stream, self._route = self._stream, None, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            logger.debug('closing the stream: %r', exc)
        logger.info('%s stopped', self.label)

    # the system default
    def _take_default(self, loopback: coreaudio.AudioDevice,
                      devices: list[coreaudio.AudioDevice]) -> None:
        ca = self.engine._ca
        current = ca.default_device(self.side)
        if current == loopback.id:
            return
        previous = next((d for d in devices if d.id == current), None)
        # Coming from the other BlackHole (7.1 sources turned on or off),
        # the device to give back is still the one saved before it.
        if previous is not None and previous.name not in LOOPBACKS:
            self.restore_file.parent.mkdir(parents=True, exist_ok=True)
            self.restore_file.write_text(json.dumps({'uid': previous.uid, 'name': previous.name}))
        ca.set_default_device(self.side, loopback.id)
        logger.info('System %s: %s -> %s', self.side, previous.name if previous else current,
                    loopback.name)

    def _restore_default(self, devices: list[coreaudio.AudioDevice]) -> None:
        """Put the system default back, if it is still on a BlackHole.

        To where it was before, else anything real that is plugged in. Left
        alone when the user already chose another device.
        """
        ca = self.engine._ca
        current = ca.default_device(self.side)
        if not any(d.id == current and d.name in LOOPBACKS for d in devices):
            self.restore_file.unlink(missing_ok=True)
            return
        try:
            saved = json.loads(self.restore_file.read_text()).get('uid')
        except (OSError, ValueError, AttributeError):
            saved = None
        others = [d for d in devices if d.name not in LOOPBACKS]
        target = next((d for d in others if d.uid == saved), None) or (others[0] if others else None)
        if target is None:
            return
        ca.set_default_device(self.side, target.id)
        self.restore_file.unlink(missing_ok=True)
        logger.info('System %s restored: %s', self.side, target.name)


class _OutputRoute(_Route):
    """BlackHole 2ch (16ch for 7.1) -> the Game channel's chain -> the headset."""

    def __init__(self, engine: 'SoftEqEngine'):
        super().__init__(engine)
        #: From the loopback's channels to SPEAKERS, for 7.1 sources; None
        #: for stereo, which goes through as it comes.
        self._mix: np.ndarray | None = None
        self._surround = False
        self.status['layout'] = ''

    def wanted(self, config: dict) -> bool:
        return config['enabled'] and eq_mode() == 'sonar'

    def loopback_name(self, config: dict) -> str:
        return loopbacks(config)[0]

    def tick(self, config: dict, devices: dict) -> None:
        self._surround = config['surround']
        super().tick(config, devices)
        if not self.status['running']:
            self.status['layout'] = ''

    def physical(self, config, devices):
        wanted = config['output_device'] or self.engine._fallback_output()
        found = pick_output(devices['output'], wanted)
        return found, ('Headset output not found' if not wanted else f'Output not found: {wanted}')

    def ends(self, loopback, physical):
        if not self._surround:
            self._mix = None
            return (loopback.name, 2), (physical, 2)
        try:
            labels = self.engine._ca.channel_layout(loopback.id)
        except OSError as exc:
            logger.warning('could not read the speakers of %s: %r', loopback.name, exc)
            labels = []
        mix, layout = speaker_map(labels)
        layout = layout or 'unset'
        if layout != self.status['layout']:
            logger.info('7.1 sources: %s is set up as %s', loopback.name, layout)
        self._mix = mix
        self.status['layout'] = layout
        return (loopback.name, mix.shape[0]), (physical, 2)

    def callback(self, indata, outdata, frames, time, status) -> None:
        cascade, gain, compressor, renderer = self.engine._chain
        mix = self._mix
        x = np.array(indata, dtype=np.float32) if mix is None or mix.shape[0] != indata.shape[1] \
            else indata @ mix
        if x.shape[1] != renderer.inputs or x.shape[1] != cascade.channels:
            outdata.fill(0)             # a reload between two stream shapes
            return
        cascade.process(x)
        if gain != 1.0:
            x *= gain
        if compressor is not None:
            compressor.process(x)
        outdata[:] = renderer.process(x)


class _MicRoute(_Route):
    """The headset's microphone -> the Micro channel's chain -> BlackHole 16ch
    (2ch with 7.1 sources)."""

    side = 'input'
    label = 'Soft EQ microphone'

    @property
    def restore_file(self) -> Path:
        return MIC_RESTORE_FILE

    def wanted(self, config: dict) -> bool:
        return config['mic_enabled']

    def loopback_name(self, config: dict) -> str:
        return loopbacks(config)[1]

    def physical(self, config, devices):
        wanted = config['mic_input_device'] or self.engine._fallback_mic()
        found = pick_mic(devices['input'], wanted)
        return found, ('Headset microphone not found' if not wanted
                       else f'Microphone not found: {wanted}')

    def ends(self, loopback, physical):
        # Mono in; the voice on both channels apps may read.
        return (physical, 1), (loopback.name, 2)

    def callback(self, indata, outdata, frames, time, status) -> None:
        cascade, gain, gate, compressor = self.engine._mic_chain
        voice = np.ascontiguousarray(indata[:, :1])
        cascade.process(voice)
        if gain != 1.0:
            voice *= gain
        if gate is not None:
            gate.process(voice)
        if compressor is not None:
            compressor.process(voice)
        outdata[:] = voice


class SoftEqEngine:
    """Owns the output and microphone routes and their DSP chains.

    tick() decides; it is cheap when nothing changed and the daemon calls
    it every couple of seconds. reload() takes a new curve or config at
    once. Both run on the daemon's loop; the audio callbacks run on
    PortAudio's threads and only read self._chain / self._mic_chain, each
    swapped as one tuple.
    """

    def __init__(self, sounddevice=None, ca=coreaudio, hrir_id=lambda: None,
                 fallback_output=lambda: '', fallback_mic=lambda: ''):
        self._sd = sounddevice
        self._ca = ca
        self._hrir_id = hrir_id
        # Where to play, and listen, when "Play on" / "Microphone" are left on
        # automatic and there is no headset to find: generic mode's devices.
        self._fallback_output = fallback_output
        self._fallback_mic = fallback_mic
        self._hrir_path = None
        self._chain = (StereoBiquads(build_chain([], 0, 0, 0)[0]), 1.0, None, Renderer())
        self._mic_chain = (StereoBiquads(np.zeros((0, 6)), channels=1), 1.0, None, None)
        self.output = _OutputRoute(self)
        self.mic = _MicRoute(self)
        self._routes = (self.output, self.mic)
        self._refreshed = False

    @property
    def status(self) -> dict:
        """The output route's, with the microphone's under 'mic'."""
        return {**self.output.status, 'mode': eq_mode(), 'mic': dict(self.mic.status)}

    # ── what the daemon calls ────────────────────────────────────────────

    def reload(self) -> dict:
        self._load_chain()
        self._load_mic_chain()
        self.tick()
        return self.status

    def tick(self) -> None:
        config = load_config()
        try:
            devices = {'output': self._ca.devices('output'), 'input': self._ca.devices('input')}
        except OSError as exc:
            for route in self._routes:
                route.stop(error=f'CoreAudio: {exc}')
            return
        for route in self._routes:
            route.tick(config, devices)
        if self._refreshed:
            # Refreshing PortAudio's device list closed every stream: reopen
            # the routes that were running before it.
            self._refreshed = False
            for route in self._routes:
                route.tick(config, devices)

    def stop(self) -> None:
        """Daemon shutdown: give the system devices back."""
        for route in self._routes:
            route.stop(error='')

    async def run(self, interval: float = 2.0) -> None:
        self._load_chain()
        self._load_mic_chain()
        try:
            while True:
                try:
                    self.tick()
                except Exception as exc:     # never take the daemon down
                    logger.warning('soft EQ tick failed: %r', exc)
                await asyncio.sleep(interval)
        finally:
            self.stop()

    # ── PortAudio ────────────────────────────────────────────────────────

    def _sounddevice(self):
        if self._sd is None:
            import sounddevice
            self._sd = sounddevice
        return self._sd

    def _device_index(self, name: str, kind: str, channels: int) -> int:
        """PortAudio's index for a device, refreshing its list if needed.

        PortAudio lists devices once, so a headset plugged in since is not
        in it. Refreshing is only safe with no stream open: the other
        route's stream is closed for it, and tick() reopens it.
        """
        sd = self._sounddevice()
        try:
            return _device_index(sd, name, kind, channels)
        except LookupError:
            for route in self._routes:
                route.close()
            sd._terminate()
            sd._initialize()
            self._refreshed = True
            return _device_index(sd, name, kind, channels)

    # ── the chains ───────────────────────────────────────────────────────

    def _load_chain(self) -> None:
        from arctis_sound_manager.sonar_to_pipewire import _load_eq_state
        state = _load_eq_state(CHANNEL)
        boost_db = state['boost_db'] if state else 0.0
        if state is None:
            sos, gain = build_chain([], 0, 0, 0)
            params = None
        else:
            sos, gain = build_chain(state['bands'], state['basses_db'], state['voix_db'],
                                    state['aigus_db'], state['boost_db'])
            params = smart_volume_params(state['smart_volume'])
        compressor = _carried(Compressor(params) if params else None, self._chain[2])
        speakers = SPEAKERS if load_config()['surround'] else STEREO
        renderer = self._load_renderer(speakers, boost_db)
        self._chain = (StereoBiquads(sos, channels=len(speakers)), gain, compressor, renderer)
        spatial = renderer.spatializer
        logger.info('Soft EQ curve: %d filters, gain %.2f dB, Smart Volume %s, %d speakers, '
                    'Spatial %s', len(sos), 20 * math.log10(gain), 'on' if compressor else 'off',
                    len(speakers), (self._hrir_path.stem + (' (surround only)' if renderer.auto
                                                            else '')) if spatial else 'off')

    def _load_mic_chain(self) -> None:
        from arctis_sound_manager.sonar_to_pipewire import _load_micro_state
        state = _load_micro_state()
        if state is None:
            sos, gain, gate_params, comp_params = np.zeros((0, 6)), 1.0, None, None
        else:
            sos, gain, gate_params, comp_params = build_mic_chain(
                state['bands'], state['basses_db'], state['voix_db'], state['aigus_db'],
                state['boost_db'], state['noise_reduction'])
        gate = Gate(gate_params['threshold']) if gate_params else None
        previous_gate = self._mic_chain[2]
        if gate and previous_gate:
            gate.gain, gate._hold_left = previous_gate.gain, previous_gate._hold_left
        compressor = _carried(Compressor(comp_params) if comp_params else None, self._mic_chain[3])
        self._mic_chain = (StereoBiquads(sos, channels=1), gain, gate, compressor)
        logger.info('Soft EQ microphone: %d filters, gain %.2f dB, gate %s, compressor %s',
                    len(sos), 20 * math.log10(gain), 'on' if gate else 'off',
                    'on' if compressor else 'off')

    def _load_renderer(self, speakers: tuple, boost_db: float) -> Renderer:
        """Spatial Audio, the Speakers mode's downmix, or the plain one."""
        settings = spatial_settings()
        previous = self._chain[3]
        if settings is None:
            return Renderer(speakers)
        if settings['mode'] == 'speakers':
            return Renderer(speakers, downmix_matrix(speakers, settings['gains']))
        if settings['auto'] and speakers == STEREO:
            return Renderer(speakers)       # a stereo source is never surround
        spatializer = self._load_spatializer(settings, speakers, boost_db)
        if spatializer is None:
            return Renderer(speakers)
        return Renderer(speakers, spatializer=spatializer, auto=settings['auto'],
                        block=BLOCK_FRAMES).carry(previous)

    def _load_spatializer(self, settings: dict, speakers: tuple,
                          boost_db: float) -> Spatializer | None:
        """The Spatial stage, kept across reloads while its HRIR and speakers
        are the same.

        Rebuilding it drops the tail it is convolving, which would be heard
        on every EQ edit; the levels change in place instead.
        """
        from arctis_sound_manager.hrir_catalog import package_hrir_path
        path = (package_hrir_path(self._hrir_id() or DEFAULT_HRIR_ID)
                or package_hrir_path(DEFAULT_HRIR_ID))
        previous = self._chain[3].spatializer
        if previous is None or path != self._hrir_path or previous.speakers != speakers:
            try:
                hrir, rate = load_wav(path)
                if rate != SAMPLE_RATE:
                    raise ValueError(f'{rate} Hz, the engine runs at {SAMPLE_RATE}')
                previous = Spatializer(hesuvi_impulses(hrir, speakers), block=BLOCK_FRAMES,
                                       speakers=speakers)
            except (OSError, ValueError, TypeError) as exc:
                logger.warning('Spatial Audio off, HRIR %s unusable: %s', path, exc)
                return None
            self._hrir_path = path
        previous.set_levels(settings['immersion'], settings['gains'])
        previous.set_distance(settings['distance'])
        previous.set_boost(boost_db)
        return previous


def _carried(compressor: Compressor | None, previous: Compressor | None) -> Compressor | None:
    """Same sound going in: keep the level the old compressor was following,
    and ramp from the gain it had rather than jumping."""
    if compressor and previous:
        compressor.envelope, compressor.gain = previous.envelope, previous.gain
    return compressor


def _device_index(sd, name: str, kind: str, channels: int) -> int:
    key = 'max_input_channels' if kind == 'input' else 'max_output_channels'
    for index, device in enumerate(sd.query_devices()):
        if device['name'] == name and device[key] >= channels:
            return index
    raise LookupError(f'no {kind} device named {name!r}')

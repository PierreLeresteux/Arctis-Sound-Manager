# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""Stand-ins for the audio managers where there is no PipeWire (macOS).

CoreEngine drives the headset and the audio graph together. Where the graph
does not exist, these answer every call the engine makes as an audio server
with no sinks, no sources and no loopbacks would, so the headset code runs
unchanged and every audio step finds nothing to do.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace


class NullAudioManager:
    """PulseAudioManager's interface, with no audio server behind it."""

    def __init__(self) -> None:
        self.logger = logging.getLogger('NullAudioManager')
        self.pulse = SimpleNamespace(source_list=lambda: [], sink_list=lambda: [])

    def sink_list_wrapper(self) -> list:
        return []

    def get_arctis_sinks_classified(self, *args, **kwargs) -> tuple[None, None]:
        return None, None

    def redirect_audio(self, output_sink_node_name: str) -> None:
        pass

    def get_default_device(self) -> None:
        return None

    def get_physical_source(self, *args, **kwargs) -> None:
        return None

    def has_source(self, node_name: str) -> bool:
        return False

    def set_default_source(self, name: str) -> None:
        pass

    def set_sink_volume_by_node(self, node_name: str, pct: int) -> bool:
        return False

    def set_mix(self, media_mix: int, chat_mix: int) -> None:
        pass


class NullLoopbackManager:
    """LoopbackManager's interface, with no pw-loopback to run."""

    def specs(self) -> dict:
        return {}

    def recreate(self, spec) -> None:
        pass

    def recreate_all(self, specs) -> None:
        pass

    def stop_all(self) -> None:
        pass

    def is_running(self, channel: str) -> bool:
        return False

    def restart_dead(self, skip_channels=None) -> list[str]:
        return []

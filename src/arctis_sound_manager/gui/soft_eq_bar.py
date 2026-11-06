# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""The macOS software equaliser's switches, device pickers and status.

Sits on top of the Sonar EQ on macOS, where the Game curve runs in the
daemon's soft_eq on the Mac's sound and the Micro one on the microphone
(through BlackHole) instead of in PipeWire filter-chains. The switches and
devices go to soft_eq.json; the daemon is told to pick them up and
reports back what it is doing, 7.1 sources' speaker layout included.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

import arctis_sound_manager.gui.theme as _theme
from arctis_sound_manager import soft_eq
from arctis_sound_manager.gui.dbus_wrapper import DbusWrapper
from arctis_sound_manager.gui.qt_widgets.no_wheel_combo import NoWheelComboBox
from arctis_sound_manager.gui.qt_widgets.q_toggle import QToggle
from arctis_sound_manager.i18n import I18n

logger = logging.getLogger(__name__)


def _t(key: str) -> str:
    return I18n.translate("ui", key)


class SoftEqBar(QFrame):
    _sig_status = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("softEqBar")
        config = soft_eq.load_config()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(12)
        self._toggle = QToggle(is_checkbox=True)
        self._toggle.setChecked(config["enabled"])
        self._toggle.stateChanged.connect(self._on_changed)
        row.addWidget(self._toggle)
        self._title = QLabel(_t("soft_eq_enable"))
        row.addWidget(self._title)
        row.addStretch(1)
        self._output_label = QLabel(_t("soft_eq_output"))
        row.addWidget(self._output_label)
        self._output = NoWheelComboBox()
        self._output.setMinimumWidth(220)
        self._output.activated.connect(self._on_changed)
        row.addWidget(self._output)
        layout.addLayout(row)

        surround_row = QHBoxLayout()
        surround_row.setSpacing(12)
        self._surround = QToggle(is_checkbox=True)
        self._surround.setChecked(config["surround"])
        self._surround.stateChanged.connect(self._on_changed)
        surround_row.addWidget(self._surround)
        self._surround_title = QLabel(_t("soft_eq_surround"))
        self._surround_title.setToolTip(_t("soft_eq_surround_hint"))
        surround_row.addWidget(self._surround_title)
        surround_row.addStretch(1)
        layout.addLayout(surround_row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        mic_row = QHBoxLayout()
        mic_row.setSpacing(12)
        self._mic_toggle = QToggle(is_checkbox=True)
        self._mic_toggle.setChecked(config["mic_enabled"])
        self._mic_toggle.stateChanged.connect(self._on_changed)
        mic_row.addWidget(self._mic_toggle)
        self._mic_title = QLabel(_t("soft_eq_mic_enable"))
        mic_row.addWidget(self._mic_title)
        mic_row.addStretch(1)
        self._mic_label = QLabel(_t("soft_eq_mic_input"))
        mic_row.addWidget(self._mic_label)
        self._mic = NoWheelComboBox()
        self._mic.setMinimumWidth(220)
        self._mic.activated.connect(self._on_changed)
        mic_row.addWidget(self._mic)
        layout.addLayout(mic_row)

        self._mic_status = QLabel("")
        self._mic_status.setWordWrap(True)
        layout.addWidget(self._mic_status)

        self._fill_devices(config["output_device"], config["mic_input_device"])
        self._sig_status.connect(self._on_status)
        # Devices come and go and the daemon may fail later (headset
        # unplugged), so the status is re-read while the page is shown.
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._refresh)
        self.apply_theme()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh()
        self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def _fill_devices(self, output: str, mic: str) -> None:
        from arctis_sound_manager import coreaudio
        for combo, side, wanted in ((self._output, "output", output), (self._mic, "input", mic)):
            try:
                names = [d.name for d in coreaudio.devices(side) if d.name not in soft_eq.LOOPBACKS]
            except OSError as exc:
                logger.warning("could not list the %ss: %r", side, exc)
                names = []
            if wanted and wanted not in names:
                names.append(wanted)
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(_t("soft_eq_output_auto"), "")
            for name in names:
                combo.addItem(name, name)
            combo.setCurrentIndex(max(0, combo.findData(wanted)))
            combo.blockSignals(False)

    def _on_changed(self, *_args) -> None:
        soft_eq.save_config({"enabled": self._toggle.isChecked(),
                             "output_device": self._output.currentData() or "",
                             "mic_enabled": self._mic_toggle.isChecked(),
                             "mic_input_device": self._mic.currentData() or "",
                             "surround": self._surround.isChecked()})
        DbusWrapper.reload_soft_eq(self._sig_status)

    def _refresh(self) -> None:
        self._fill_devices(self._output.currentData() or "", self._mic.currentData() or "")
        DbusWrapper.get_soft_eq_status(self._sig_status)

    @Slot(object)
    def _on_status(self, status: dict) -> None:
        # The BlackHoles as the switch is now, until the daemon says.
        out_loop, mic_loop = soft_eq.loopbacks({"surround": self._surround.isChecked()})
        mic = status.get("mic") or {}
        out_loop = status.get("loopback") or out_loop
        mic_loop = mic.get("loopback") or mic_loop
        if not status.get("available", True):
            text = _t("soft_eq_no_daemon")
        elif status.get("error"):
            text = status["error"]
        elif status.get("mode") == "custom":
            text = _t("soft_eq_custom_mode")
        elif status.get("running"):
            text = _t("soft_eq_running").format(output=status.get("output", ""), loopback=out_loop)
            layout = status.get("layout")
            if layout == "unset":
                text += "\n" + _t("soft_eq_layout_unset")
            elif layout:
                text += "\n" + _t("soft_eq_layout").format(layout=layout)
        else:
            text = _t("soft_eq_off").format(loopback=out_loop)
        self._status.setText(text)

        if not status.get("available", True):
            mic_text = ""
        elif mic.get("error"):
            mic_text = mic["error"]
        elif mic.get("running"):
            mic_text = _t("soft_eq_mic_running").format(input=mic.get("input", ""),
                                                        loopback=mic_loop)
        else:
            mic_text = _t("soft_eq_mic_off").format(loopback=mic_loop)
        self._mic_status.setText(mic_text)

    def apply_theme(self, t=None) -> None:
        self.setStyleSheet(f"""
            QFrame#softEqBar {{
                background-color: {_theme.c('BG_CARD')};
                border: 1px solid {_theme.c('BORDER')};
                border-radius: 10px;
            }}
        """)
        for title in (self._title, self._surround_title, self._mic_title):
            title.setStyleSheet(
                f"color: {_theme.c('TEXT_PRIMARY')}; font-size: 11pt; font-weight: bold; "
                f"background: transparent;")
        for label in (self._output_label, self._status, self._mic_label, self._mic_status):
            label.setStyleSheet(
                f"color: {_theme.c('TEXT_SECONDARY')}; font-size: 9pt; background: transparent;")

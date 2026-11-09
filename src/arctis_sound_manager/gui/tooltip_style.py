# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tooltips in the theme's colours, whatever widget they belong to.

The app stylesheet has a ``QToolTip`` rule, but a tooltip is styled with
the stylesheets of the widget it belongs to too, and those win over the
app's. Many widgets set a selector-less sheet on themselves, such as
``background: transparent;``: the tooltip inherits it and turns
transparent, and macOS draws its native light tooltip behind it, so a dark
theme shows light tooltips with light text.

Qt resets each tooltip's own stylesheet as it places the tip
(``QTipLabel::placeTip``). This filter sets it back to the theme's rule
right away: a widget's own sheet beats every other.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget

from arctis_sound_manager.gui.theme import tooltip_qss

_TIP_CLASS = 'QTipLabel'
_EVENTS = (QEvent.Type.StyleChange, QEvent.Type.Show)


class TooltipStyler(QObject):
    """Application event filter that keeps every tooltip on the theme."""

    def __init__(self, theme_name: str, parent: QObject | None = None):
        super().__init__(parent)
        self._qss = tooltip_qss(theme_name)

    def set_theme(self, theme_name: str) -> None:
        self._qss = tooltip_qss(theme_name)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt)
        if (event.type() in _EVENTS and isinstance(obj, QWidget)
                and obj.metaObject().className() == _TIP_CLASS
                and obj.styleSheet() != self._qss):
            obj.setStyleSheet(self._qss)
        return False


def install(app: QApplication, theme_name: str) -> TooltipStyler:
    """Install the styler on *app*; keep the result to change its theme."""
    styler = TooltipStyler(theme_name, app)
    app.installEventFilter(styler)
    return styler

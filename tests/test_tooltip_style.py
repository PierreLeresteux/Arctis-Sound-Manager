# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tooltips keep the theme's colours under a widget with its own stylesheet.

A selector-less sheet on a widget (``background: transparent;``) reaches
its tooltip and wins over the app's ``QToolTip`` rule: on macOS the tip
turned transparent over the native light one, light text on light.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint
from PySide6.QtWidgets import QApplication, QPushButton, QToolTip, QVBoxLayout, QWidget

from arctis_sound_manager.gui import tooltip_style
from arctis_sound_manager.gui.theme import build_qss, tooltip_qss


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    old = app.styleSheet()
    app.setStyleSheet(build_qss('steelseries'))
    styler = tooltip_style.install(app, 'steelseries')
    yield app, styler
    # Drop the tip before the widgets it points at, and leave no filter or
    # sheet behind for the next tests.
    for tip in _tips():
        tip.hide()
        tip.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.removeEventFilter(styler)
    styler.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.setStyleSheet(old)
    for bar in _bars:
        bar.deleteLater()
    _bars.clear()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


_bars: list[QWidget] = []


def _tips():
    return [w for w in QApplication.topLevelWidgets() if w.metaObject().className() == 'QTipLabel']


def _transparent_bar(*labels):
    bar = QWidget()
    bar.setStyleSheet('background: transparent;')
    layout = QVBoxLayout(bar)
    buttons = [QPushButton(text) for text in labels]
    for b in buttons:
        b.setToolTip(f'{b.text()} tip')
        layout.addWidget(b)
    bar.show()
    QApplication.processEvents()
    _bars.append(bar)
    return bar, buttons


def _show(button):
    QToolTip.showText(button.mapToGlobal(QPoint(2, 2)), button.toolTip(), button)
    QApplication.processEvents()
    return next(w for w in _tips() if w.isVisible())


def test_the_app_stylesheet_carries_the_tooltip_rule():
    assert tooltip_qss('steelseries') in build_qss('steelseries')


def test_a_tooltip_under_a_transparent_parent_gets_the_theme_rule(qapp):
    bar, (save,) = _transparent_bar('Save')
    tip = _show(save)
    assert tip.styleSheet() == tooltip_qss('steelseries')


def test_a_reused_tooltip_keeps_the_rule(qapp):
    # Moving to another widget while the tip shows reuses it, and Qt resets
    # its sheet as it places it again.
    bar, (save, save_as) = _transparent_bar('Save', 'Save as')
    _show(save)
    tip = _show(save_as)
    assert tip.text() == 'Save as tip'
    assert tip.styleSheet() == tooltip_qss('steelseries')


def test_a_theme_change_reaches_the_next_tooltip(qapp):
    _, styler = qapp
    styler.set_theme('arctic')
    bar, (save,) = _transparent_bar('Save')
    assert _show(save).styleSheet() == tooltip_qss('arctic')


def test_other_widgets_are_left_alone(qapp):
    bar, (save,) = _transparent_bar('Save')
    QApplication.processEvents()
    assert save.styleSheet() == ''
    assert bar.styleSheet() == 'background: transparent;'

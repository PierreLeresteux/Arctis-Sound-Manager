# -*- mode: python -*-
# PyInstaller spec for ASM.app, the self-contained macOS app.
# Built by macos/build-app.sh; see docs/macos.md.
#
# One bundle, three executables sharing one copy of Python and Qt in
# Contents/MacOS: asm-gui (what the app opens), asm-daemon (started by the
# GUI, or by its LaunchAgent) and asm-cli.

import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
SCRIPTS = ROOT / 'src' / 'arctis_sound_manager' / 'scripts'
VERSION = re.search(r'^version = "(.+)"', (ROOT / 'pyproject.toml').read_text(), re.M)[1]

# Device profiles, translations, images, presets, HRIRs: everything the
# package reads next to its modules. Many modules are imported inside
# functions or by name, so take them all rather than chase each one.
datas = collect_data_files('arctis_sound_manager') + copy_metadata('arctis-sound-manager')
hiddenimports = collect_submodules('arctis_sound_manager')

# pyusb loads libusb through ctypes, which PyInstaller cannot see: ship
# Homebrew's copy so the app does not need it installed.
LIBUSB = Path('/opt/homebrew/lib/libusb-1.0.dylib')
if not LIBUSB.exists():
    LIBUSB = Path('/usr/local/lib/libusb-1.0.dylib')
binaries = [(str(LIBUSB.resolve()), '.')]

# Linux-only, or never reached on macOS.
excludes = ['pulsectl', 'pyudev', 'tkinter',
            'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQml',
            'PySide6.QtQuick', 'PySide6.Qt3DCore', 'PySide6.QtMultimedia']


def program(name, script):
    analysis = Analysis([str(SCRIPTS / script)], pathex=[str(ROOT / 'src')],
                        binaries=binaries, datas=datas, hiddenimports=hiddenimports,
                        excludes=excludes)
    exe = EXE(PYZ(analysis.pure), analysis.scripts, [], exclude_binaries=True,
              name=name, console=False, codesign_identity=None)
    return analysis, exe


gui, gui_exe = program('asm-gui', 'gui.py')
daemon, daemon_exe = program('asm-daemon', 'daemon.py')
cli, cli_exe = program('asm-cli', 'cli.py')

app_files = COLLECT(gui_exe, gui.binaries, gui.datas,
                    daemon_exe, daemon.binaries, daemon.datas,
                    cli_exe, cli.binaries, cli.datas,
                    name='ASM')

app = BUNDLE(
    app_files,
    name='Arctis Sound Manager.app',
    icon=str(ROOT / 'build' / 'ASM.icns'),
    bundle_identifier='com.github.arctis-sound-manager',
    version=VERSION,
    info_plist={
        'CFBundleName': 'Arctis Sound Manager',
        'CFBundleDisplayName': 'Arctis Sound Manager',
        'CFBundleShortVersionString': VERSION,
        # BUNDLE would take the last executable in the COLLECT.
        'CFBundleExecutable': 'asm-gui',
        'NSHighResolutionCapable': True,
        # A menu bar app: no Dock icon, the window opens from the icon.
        'LSUIElement': True,
        # The soft EQ reads the Mac's sound from BlackHole, an input device:
        # without this, macOS denies the bundle any input without asking,
        # and the equaliser plays silence (a terminal has its own grant).
        'NSMicrophoneUsageDescription':
            "Arctis Sound Manager reads the Mac's sound from BlackHole "
            "to run the equaliser on it.",
        'LSMinimumSystemVersion': '13.0',
    },
)

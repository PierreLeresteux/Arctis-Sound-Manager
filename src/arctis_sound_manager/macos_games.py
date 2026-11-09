# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""The game running on the Mac, for the automatic Sonar preset per game.

On Linux a game is found by its audio stream and its launcher's
environment (game_presets). macOS has neither a per-app audio graph nor a
way to read another app's environment, so this looks at the user's running
processes instead:

* **Steam**: an executable under a library's ``steamapps/common/<dir>/`` is
  the game whose ``appmanifest_<appid>.acf`` installs to ``<dir>``. The key
  is ``steam:<appid>``, the same as on Linux, so a preset tied to a game on
  one follows it on the other.
* **Any other game**: an app whose Info.plist files it under Games
  (``LSApplicationCategoryType`` ``public.app-category.games`` or one of
  its ``*-games`` subcategories): the Mac App Store, Apple Arcade, and most
  games sold outside it. The key is ``app:<bundle id>``.

Launchers file themselves under Games too (Steam, Apple's Games app, Epic,
Battle.net...), and so do cloud and remote play clients: they are left out, or the launcher would win over the game
it starts. With several games running, a Steam game wins, then the most
recently started.
"""

from __future__ import annotations

import logging
import plistlib
import subprocess
from pathlib import Path

from arctis_sound_manager.game_presets import DetectedGame, parse_vdf

logger = logging.getLogger(__name__)

STEAM_ROOTS = (Path.home() / 'Library' / 'Application Support' / 'Steam',)

#: Apps filed under Games that start games rather than being one.
LAUNCHERS = frozenset({
    'com.valvesoftware.steam',
    'com.apple.games',
    'com.epicgames.epicgameslauncher',
    'net.battle.app',
    'net.battle.bootstrapper',
    'com.blizzard.battle.net',
    'com.gog.galaxy',
    'com.heroicgameslauncher.hgl',
    'com.ea.app',
    'com.ubisoft.connect',
    # Streaming and remote play: the game runs elsewhere, and their helper
    # keeps running in the background with no game open.
    'tv.parsec.www',
    'com.nvidia.gfnpc.mall',
})

_GAMES = 'public.app-category.games'
_BUNDLE_SPLIT = '.app/Contents/MacOS/'
_PS = ('/bin/ps', '-x', '-o', 'pid=,comm=')

# Info.plist answers by bundle path: an app does not change category while
# it runs, and the watcher asks every few seconds.
_bundle_cache: dict[str, tuple[str, str] | None] = {}


def running_processes() -> list[tuple[int, str]]:
    """(pid, executable path) of the user's processes."""
    try:
        out = subprocess.run(_PS, capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug('ps failed: %r', exc)
        return []
    processes = []
    for line in out.splitlines():
        pid, _, path = line.strip().partition(' ')
        if pid.isdigit() and path.strip():
            processes.append((int(pid), path.strip()))
    return processes


# ── Steam ───────────────────────────────────────────────────────────────────

def _steam_libraries(roots: tuple[Path, ...]) -> list[Path]:
    libraries = []
    for root in roots:
        libraries.append(root)
        try:
            text = (root / 'steamapps' / 'libraryfolders.vdf').read_text(errors='replace')
        except OSError:
            continue
        folders = next(iter(parse_vdf(text).values()), {})
        if isinstance(folders, dict):
            for entry in folders.values():
                path = entry.get('path') if isinstance(entry, dict) else None
                if path and Path(path) not in libraries:
                    libraries.append(Path(path))
    return libraries


def steam_installs(roots: tuple[Path, ...] | None = None) -> dict[Path, tuple[str, str]]:
    """Each installed Steam game's folder -> (appid, name)."""
    installs = {}
    for library in _steam_libraries(STEAM_ROOTS if roots is None else roots):
        apps = library / 'steamapps'
        try:
            manifests = list(apps.glob('appmanifest_*.acf'))
        except OSError:
            continue
        for manifest in manifests:
            try:
                state = next(iter(parse_vdf(manifest.read_text(errors='replace')).values()), {})
            except OSError:
                continue
            if not isinstance(state, dict):
                continue
            appid, folder = state.get('appid'), state.get('installdir')
            if isinstance(appid, str) and appid.isdigit() and isinstance(folder, str) and folder:
                name = state.get('name') if isinstance(state.get('name'), str) else folder
                installs[apps / 'common' / folder] = (appid, name.strip() or folder)
    return installs


def _steam_game(path: str, installs: dict[Path, tuple[str, str]]) -> tuple[str, str] | None:
    exe = Path(path)
    for folder, game in installs.items():
        if exe.is_relative_to(folder):
            return game
    return None


# ── apps filed under Games ──────────────────────────────────────────────────

def bundle_of(path: str) -> str | None:
    """The .app an executable runs from (the outermost one), if any."""
    head, sep, _ = path.partition(_BUNDLE_SPLIT)
    return head + '.app' if sep else None


def _is_games_category(category: str) -> bool:
    return category == _GAMES or (category.startswith('public.app-category.')
                                  and category.endswith('-games'))


def game_app(bundle: str) -> tuple[str, str] | None:
    """(bundle id, name) when the app is a game, else None. Cached."""
    if bundle in _bundle_cache:
        return _bundle_cache[bundle]
    found = None
    try:
        with open(Path(bundle) / 'Contents' / 'Info.plist', 'rb') as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        info = {}
    bundle_id = str(info.get('CFBundleIdentifier') or '')
    if (bundle_id and _is_games_category(str(info.get('LSApplicationCategoryType') or ''))
            and bundle_id.lower() not in LAUNCHERS):
        name = (info.get('CFBundleDisplayName') or info.get('CFBundleName')
                or Path(bundle).stem)
        found = (bundle_id, str(name).strip() or Path(bundle).stem)
    _bundle_cache[bundle] = found
    return found


# ── the game ────────────────────────────────────────────────────────────────

def detect_current_game(processes: list[tuple[int, str]] | None = None,
                        steam_roots: tuple[Path, ...] | None = None) -> DetectedGame | None:
    """The game running now: a Steam game first, then the newest game app."""
    processes = running_processes() if processes is None else processes
    installs = steam_installs(steam_roots)
    steam, apps = [], []
    for pid, path in processes:
        if installs and (game := _steam_game(path, installs)):
            steam.append(DetectedGame(f'steam:{int(game[0])}', game[1], pid))
            continue
        bundle = bundle_of(path)
        if bundle and (app := game_app(bundle)):
            apps.append(DetectedGame(f'app:{app[0]}', app[1], pid))
    for found in (steam, apps):
        if found:
            return max(found, key=lambda g: g.pid or 0)
    return None

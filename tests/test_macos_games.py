# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""macOS: the game running now, for the automatic Sonar preset per game."""

from __future__ import annotations

import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from arctis_sound_manager import game_presets, macos_games
from arctis_sound_manager.game_presets import DetectedGame


@pytest.fixture(autouse=True)
def _fresh_cache():
    macos_games._bundle_cache.clear()
    yield
    macos_games._bundle_cache.clear()


def _app(root, name, bundle_id, category, display=None, exe=None):
    bundle = root / f'{name}.app'
    (bundle / 'Contents' / 'MacOS').mkdir(parents=True)
    info = {'CFBundleIdentifier': bundle_id, 'CFBundleName': name}
    if category:
        info['LSApplicationCategoryType'] = category
    if display:
        info['CFBundleDisplayName'] = display
    with open(bundle / 'Contents' / 'Info.plist', 'wb') as f:
        plistlib.dump(info, f)
    return str(bundle / 'Contents' / 'MacOS' / (exe or name))


def _steam(root, appid, folder, name, library=None):
    apps = (library or root) / 'steamapps'
    (apps / 'common' / folder).mkdir(parents=True, exist_ok=True)
    (apps / f'appmanifest_{appid}.acf').write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n'
        f'\t"installdir"\t\t"{folder}"\n}}\n')
    return apps / 'common' / folder


# ── apps filed under Games ──────────────────────────────────────────────────

@pytest.mark.parametrize('category, game', [
    ('public.app-category.games', True),
    ('public.app-category.board-games', True),
    ('public.app-category.simulation-games', True),
    ('public.app-category.music', False),
    (None, False),
])
def test_an_app_is_a_game_by_its_category(tmp_path, category, game):
    exe = _app(tmp_path, 'Thing', 'com.example.thing', category)
    found = macos_games.detect_current_game([(10, exe)], steam_roots=())
    assert (found == DetectedGame('app:com.example.thing', 'Thing', 10)) is game


def test_the_display_name_is_the_title(tmp_path):
    exe = _app(tmp_path, 'jr', 'com.domain.game', 'public.app-category.games',
               display='Journey Rebirth')
    assert macos_games.detect_current_game([(1, exe)], steam_roots=()).title == 'Journey Rebirth'


@pytest.mark.parametrize('bundle_id', ['com.valvesoftware.steam', 'com.apple.games',
                                       'com.nvidia.gfnpc.mall', 'tv.parsec.www',
                                       'com.epicgames.EpicGamesLauncher'])
def test_launchers_and_streaming_clients_are_not_the_game(tmp_path, bundle_id):
    exe = _app(tmp_path, 'Launcher', bundle_id, 'public.app-category.games')
    assert macos_games.detect_current_game([(1, exe)], steam_roots=()) is None


def test_a_helper_inside_a_game_counts_as_the_game(tmp_path):
    exe = _app(tmp_path, 'Game', 'com.example.game', 'public.app-category.games')
    helper = exe.replace('/Contents/MacOS/Game',
                         '/Contents/MacOS/Helper.app/Contents/MacOS/Helper')
    assert macos_games.bundle_of(helper) == str(tmp_path / 'Game.app')
    assert macos_games.detect_current_game([(2, helper)], steam_roots=()).key == 'app:com.example.game'


def test_a_plain_executable_is_never_a_game(tmp_path):
    assert macos_games.detect_current_game([(1, '/usr/bin/python3')], steam_roots=()) is None


def test_the_newest_game_app_wins(tmp_path):
    old = _app(tmp_path, 'Old', 'com.example.old', 'public.app-category.games')
    new = _app(tmp_path, 'New', 'com.example.new', 'public.app-category.games')
    assert macos_games.detect_current_game([(50, new), (10, old)],
                                           steam_roots=()).key == 'app:com.example.new'


def test_info_plist_is_read_once_per_bundle(tmp_path, monkeypatch):
    exe = _app(tmp_path, 'Game', 'com.example.game', 'public.app-category.games')
    macos_games.detect_current_game([(1, exe)], steam_roots=())
    monkeypatch.setattr(macos_games.plistlib, 'load', lambda f: pytest.fail('read again'))
    assert macos_games.detect_current_game([(1, exe)], steam_roots=()) is not None


# ── Steam ───────────────────────────────────────────────────────────────────

def test_a_steam_game_is_found_by_its_install_folder(tmp_path):
    folder = _steam(tmp_path, '1145360', 'Hades', 'Hades')
    exe = str(folder / 'Hades.app' / 'Contents' / 'MacOS' / 'Hades')
    found = macos_games.detect_current_game([(7, exe)], steam_roots=(tmp_path,))
    # The same key as on Linux: a preset tied to the game follows it.
    assert found == DetectedGame('steam:1145360', 'Hades', 7)


def test_steam_games_in_another_library_are_found(tmp_path):
    root, other = tmp_path / 'Steam', tmp_path / 'External'
    (root / 'steamapps').mkdir(parents=True)
    (root / 'steamapps' / 'libraryfolders.vdf').write_text(
        f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{root}"\n\t}}\n'
        f'\t"1"\n\t{{\n\t\t"path"\t\t"{other}"\n\t}}\n}}\n')
    folder = _steam(root, '620', 'Portal 2', 'Portal 2', library=other)
    found = macos_games.detect_current_game([(3, str(folder / 'portal2_osx'))],
                                            steam_roots=(root,))
    assert found.key == 'steam:620'


def test_a_steam_game_wins_over_a_game_app(tmp_path):
    folder = _steam(tmp_path / 'Steam', '620', 'Portal 2', 'Portal 2')
    app = _app(tmp_path, 'Chess', 'com.apple.Chess', 'public.app-category.board-games')
    found = macos_games.detect_current_game([(90, app), (5, str(folder / 'portal2_osx'))],
                                            steam_roots=(tmp_path / 'Steam',))
    assert found.key == 'steam:620'


def test_a_steam_game_that_is_also_a_game_app_keeps_its_steam_key(tmp_path):
    folder = _steam(tmp_path, '1145360', 'Hades', 'Hades')
    exe = _app(folder, 'Hades', 'com.supergiant.hades', 'public.app-category.games')
    assert macos_games.detect_current_game([(1, exe)], steam_roots=(tmp_path,)).key \
        == 'steam:1145360'


def test_no_steam_is_fine(tmp_path):
    assert macos_games.steam_installs((tmp_path / 'nope',)) == {}


# ── the processes ───────────────────────────────────────────────────────────

def test_processes_are_read_from_ps_with_paths_containing_spaces(monkeypatch):
    out = '  12 /Applications/My Game.app/Contents/MacOS/My Game\n 3 /usr/sbin/cfprefsd\n\n'
    monkeypatch.setattr(macos_games.subprocess, 'run',
                        lambda *a, **k: SimpleNamespace(stdout=out))
    assert macos_games.running_processes() == [
        (12, '/Applications/My Game.app/Contents/MacOS/My Game'), (3, '/usr/sbin/cfprefsd')]


def test_a_failing_ps_means_no_game(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired('ps', 5)
    monkeypatch.setattr(macos_games.subprocess, 'run', boom)
    assert macos_games.running_processes() == []


def test_the_watcher_asks_macos_on_a_mac(monkeypatch):
    from arctis_sound_manager import platform_support
    monkeypatch.setattr(platform_support, 'IS_MACOS', True)
    game = DetectedGame('app:x', 'X', 1)
    monkeypatch.setattr(macos_games, 'detect_current_game', lambda: game)
    assert game_presets.detect_current_game() is game

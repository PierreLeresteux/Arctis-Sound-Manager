# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Restarting asm-daemon on macOS, where no systemctl does it: stop whoever
holds the daemon's lock, then start a new one (through launchd when its
LaunchAgent is loaded). A small process standing in for the daemon takes
the real lock, as asm-daemon does.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from arctis_sound_manager import platform_support

#: Takes the lock, writes its pid, as daemon._single_instance_lock does;
#: exits on SIGTERM unless told to ignore it.
FAKE_DAEMON = textwrap.dedent("""
    import fcntl, os, signal, sys, time
    handle = open(sys.argv[1], 'w')
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    handle.write(f'{os.getpid()}\\n'); handle.flush()
    if sys.argv[2] == 'stubborn':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.05)
""")


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    procs: list[subprocess.Popen] = []

    def spawn(mode="polite"):
        proc = subprocess.Popen([sys.executable, "-c", FAKE_DAEMON,
                                 platform_support.daemon_lock_path(), mode])
        procs.append(proc)
        deadline = time.monotonic() + 5
        while platform_support.daemon_pid() != proc.pid:
            assert time.monotonic() < deadline, "the fake daemon never took the lock"
            time.sleep(0.05)
        return proc

    monkeypatch.setattr(platform_support, "_launch_agent_loaded", lambda: False)
    monkeypatch.setattr(platform_support, "start_daemon_if_needed", lambda: spawn())
    yield spawn, procs
    for proc in procs:
        proc.kill()
        proc.wait()


def test_no_daemon_no_pid(runtime):
    assert platform_support.daemon_pid() is None


def test_a_running_daemon_is_stopped_and_a_new_one_started(runtime):
    spawn, procs = runtime
    old = spawn()

    assert platform_support.restart_daemon(timeout=5)

    assert old.wait(timeout=5) == -15                       # SIGTERM
    assert len(procs) == 2 and platform_support.daemon_pid() == procs[1].pid


def test_a_daemon_deaf_to_sigterm_is_killed(runtime):
    spawn, procs = runtime
    old = spawn("stubborn")

    assert platform_support.restart_daemon(timeout=0.5)

    assert old.wait(timeout=5) == -9                        # SIGKILL
    assert platform_support.daemon_pid() == procs[1].pid


def test_with_no_daemon_running_it_just_starts_one(runtime):
    _spawn, procs = runtime

    assert platform_support.restart_daemon(timeout=5)

    assert len(procs) == 1 and platform_support.daemon_pid() == procs[0].pid


def test_a_loaded_launch_agent_starts_it_through_launchd(runtime, monkeypatch):
    spawn, procs = runtime
    spawn()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        spawn()                                             # launchd starting it
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(platform_support, "_launch_agent_loaded", lambda: True)
    monkeypatch.setattr(platform_support.subprocess, "run", run)

    assert platform_support.restart_daemon(timeout=5)

    assert calls == [["/bin/launchctl", "kickstart",
                      f"gui/{os.getuid()}/com.github.arctis-sound-manager.daemon"]]
    assert platform_support.daemon_pid() == procs[1].pid


def test_a_daemon_that_never_comes_back_is_a_failure(runtime, monkeypatch):
    spawn, _procs = runtime
    spawn()
    monkeypatch.setattr(platform_support, "start_daemon_if_needed", lambda: None)

    assert not platform_support.restart_daemon(timeout=0.5)

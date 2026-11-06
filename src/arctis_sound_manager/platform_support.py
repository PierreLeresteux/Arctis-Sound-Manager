# Copyright (C) 2026 loteran
# SPDX-License-Identifier: GPL-3.0-or-later

"""What differs between the platforms ASM runs on.

ASM was written for Linux. On macOS only the headset control runs: the
protocol, the daemon and the GUI's device pages. Everything built on
PipeWire (the Game/Chat/Media channels, Sonar EQ, surround, Clips) has no
counterpart there, so AUDIO_SUPPORTED gates it rather than letting each
call fail on its own.

The daemon and the GUI still talk over D-Bus. Homebrew's dbus runs a
session bus per login through launchd, but advertises it as a `launchd:`
address that dbus-next cannot parse: prepare_session_bus_env() resolves it
to the unix socket behind it before the first connection.

It also works around a dbus-next bug that only macOS triggers. A unix
socket there has an 8 KiB send buffer, so a large message (the status or
the settings) fills it and send() raises BlockingIOError. dbus-next takes
that for a dead connection and closes the bus, and every later call on it
times out.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

IS_LINUX = sys.platform.startswith('linux')
IS_MACOS = sys.platform == 'darwin'

#: PipeWire and the whole audio graph ASM builds on it.
AUDIO_SUPPORTED = IS_LINUX

#: Running from ASM.app (macos/build-app.sh), where nothing else starts the
#: daemon: no systemd, and no uv checkout to run it from.
IS_APP_BUNDLE = IS_MACOS and getattr(sys, 'frozen', False)

_LAUNCHD_SOCKET_VAR = 'DBUS_LAUNCHD_SESSION_BUS_SOCKET'


def daemon_lock_path() -> str:
    """The file asm-daemon holds an exclusive flock on while it runs."""
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR') or f'/tmp/arctis-{os.getuid()}'
    return os.path.join(runtime_dir, 'arctis-manager.lock')


def script_path(name: str) -> str:
    """An ASM entry point, by absolute path.

    launchd starts agents with a bare PATH, and ASM.app has no PATH entry
    at all. The scripts sit next to the interpreter (or, in the app, the
    executable) running this one.
    """
    sibling = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.exists(sibling):
        return sibling
    return shutil.which(name) or name


def start_daemon_if_needed() -> None:
    """Start asm-daemon from the GUI, when no daemon holds its lock.

    For ASM.app, which is the only thing the user starts. The daemon runs
    in its own session, so quitting the GUI leaves it running, as a
    service would be. Its LaunchAgent may start one at the same moment at
    login: whichever loses the lock exits.
    """
    import fcntl
    try:
        with open(daemon_lock_path(), 'a') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        pass                    # no runtime dir yet: no daemon either
    except OSError:
        return                  # held: a daemon is running
    log_path = os.path.expanduser('~/Library/Logs/asm-daemon.log')
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, 'ab') as log:
            subprocess.Popen([script_path('asm-daemon')], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        logger.warning("could not start asm-daemon: %r", exc)
        return
    logger.info("Started asm-daemon (log: %s)", log_path)


#: The daemon's LaunchAgent (autostart.LaunchdBackend, docs/macos.md).
LAUNCHD_DAEMON_LABEL = 'com.github.arctis-sound-manager.daemon'


def _lock_is_held() -> bool:
    import fcntl
    try:
        with open(daemon_lock_path()) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def daemon_pid() -> int | None:
    """The running asm-daemon's pid, or None when no daemon holds the lock.

    The daemon writes its pid in the lock file; one from before it did is
    found by who has the file open.
    """
    if not _lock_is_held():
        return None
    try:
        text = open(daemon_lock_path()).read().strip()
    except OSError:
        text = ''
    if text.isdigit():
        return int(text)
    try:
        out = subprocess.run(['/usr/sbin/lsof', '-t', daemon_lock_path()], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pids = [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]
    return pids[0] if pids else None


def _wait_for_lock(held: bool, timeout: float) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while _lock_is_held() != held:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def _launch_agent_loaded() -> bool:
    try:
        return subprocess.run(['/bin/launchctl', 'print',
                               f'gui/{os.getuid()}/{LAUNCHD_DAEMON_LABEL}'],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def restart_daemon(timeout: float = 10.0) -> bool:
    """Stop the running asm-daemon and start a fresh one (macOS).

    SIGTERM first, so it shuts down cleanly (the soft EQ gives the system
    output and input back), SIGKILL if it is still there after *timeout*.
    Then its LaunchAgent starts it when one is loaded, so launchd keeps
    watching it, or the app starts it as at launch. True once a new
    daemon holds the lock.
    """
    import signal
    pid = daemon_pid()
    if pid is not None:
        logger.info("Stopping asm-daemon (pid %d)", pid)
        for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 3.0)):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                logger.warning("could not stop asm-daemon: %r", exc)
                return False
            if _wait_for_lock(False, wait):
                break
        else:
            logger.warning("asm-daemon (pid %d) did not stop", pid)
            return False
    if _launch_agent_loaded():
        try:
            subprocess.run(['/bin/launchctl', 'kickstart',
                            f'gui/{os.getuid()}/{LAUNCHD_DAEMON_LABEL}'],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("launchctl kickstart failed: %r", exc)
    else:
        start_daemon_if_needed()
    started = _wait_for_lock(True, timeout)
    logger.info("asm-daemon %s", "restarted" if started else "did not start again")
    return started


def prepare_session_bus_env() -> None:
    """Point dbus-next at the session bus launchd started, on macOS.

    Also patches dbus-next for macOS's small socket buffers (see the module
    docstring). A no-op on Linux; the address is left alone whenever
    DBUS_SESSION_BUS_ADDRESS already names a transport dbus-next understands.
    """
    if not IS_MACOS:
        return
    _patch_dbus_next_partial_writes()
    address = os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
    if address and not address.startswith('launchd:'):
        return
    socket = os.environ.get(_LAUNCHD_SOCKET_VAR) or _launchctl_getenv(_LAUNCHD_SOCKET_VAR)
    if not socket:
        logger.warning("No D-Bus session bus: install `brew install dbus` and load its "
                       "session LaunchAgent (see docs/macos.md).")
        return
    os.environ['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={socket}'


def _patch_dbus_next_partial_writes() -> None:
    """Make dbus-next wait for a full send buffer to drain instead of failing.

    A copy of dbus_next.aio.message_bus._MessageWriter.write_callback (0.2.3)
    with one more except clause: on BlockingIOError the writer stays
    registered with the loop, which calls it again once the socket is
    writable, and resumes at self.offset.
    """
    try:
        from dbus_next.aio import message_bus
    except ImportError:
        return
    writer = message_bus._MessageWriter
    if getattr(writer, '_asm_patched', False):
        return

    def write_callback(self):
        try:
            while True:
                if self.buf is None:
                    if self.messages.qsize() == 0:
                        self.loop.remove_writer(self.fd)
                        return
                    buf, unix_fds, fut = self.messages.get_nowait()
                    self.unix_fds = unix_fds
                    self.buf = memoryview(buf)
                    self.offset = 0
                    self.fut = fut

                if self.unix_fds and self.negotiate_unix_fd:
                    ancdata = [(message_bus.socket.SOL_SOCKET, message_bus.socket.SCM_RIGHTS,
                                message_bus.array.array("i", self.unix_fds))]
                    self.offset += self.sock.sendmsg([self.buf[self.offset:]], ancdata)
                    self.unix_fds = None
                else:
                    self.offset += self.sock.send(self.buf[self.offset:])

                if self.offset >= len(self.buf):
                    self.buf = None
                    message_bus._future_set_result(self.fut, None)
                else:
                    return
        except BlockingIOError:
            # Send buffer full: called again once it drains.
            return
        except Exception as e:
            message_bus._future_set_exception(self.fut, e)
            self.bus._finalize(e)

    writer.write_callback = write_callback
    writer._asm_patched = True


def _launchctl_getenv(name: str) -> str | None:
    try:
        result = subprocess.run(['launchctl', 'getenv', name], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def notify(title: str, body: str, *, critical: bool = False, app_name: str = 'Arctis Sound Manager') -> None:
    """Best-effort desktop notification, without Qt.

    notify-send on Linux, Notification Center through osascript on macOS.
    Missing either just means no notification.
    """
    if IS_MACOS:
        script = f'display notification {_applescript_string(body)} with title {_applescript_string(title)}'
        command = ['osascript', '-e', script]
    else:
        notify_send = shutil.which('notify-send')
        if not notify_send:
            return
        command = [notify_send, f'--app-name={app_name}']
        if critical:
            command.append('--urgency=critical')
        command += [title, body]
    try:
        subprocess.run(command, timeout=5, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("notification not shown: %r", exc)


def _applescript_string(text: str) -> str:
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'

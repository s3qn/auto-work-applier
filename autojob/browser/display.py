"""Manages the Xvfb + x11vnc stack behind the `/browser` view.

Two local processes, neither reachable from outside the machine (`Xvfb` runs
with `-nolisten tcp`, `x11vnc` with `-localhost`):

    Chromium (headed) --draws on--> Xvfb :99 <--reads from-- x11vnc
        --serves VNC/RFB on 127.0.0.1:<port>--> (FastAPI relays this)

`autojob.browser.context.browser_page(..., visible=True)` points Chromium at
the Xvfb display this module manages. `autojob.web.routes` relays the noVNC
WebSocket to x11vnc's port through the authenticated `/browser/ws` route, so
the only way to reach any of this from outside the machine is through the
dashboard login.

Every process is tracked by a pid file under `data/vnc/`, so `status()` and
`start_all()` work without ever asking the OS for a live process list, and
`start_all()` is idempotent: a leg whose pid file points at a live process
running the expected binary (checked against `/proc/<pid>/cmdline`, so a pid
reused after a reboot is never mistaken for ours) is left alone. Tests
monkeypatch `_spawn`, `_pid_alive`, `_pid_runs`, `_wait_for_x11_socket`, and
`_wait_for_port` rather than starting any of this for real.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from autojob.config import Config

DEFAULT_DISPLAY = ":99"
DEFAULT_GEOMETRY = "1366x900x24"
DEFAULT_X11VNC_PORT = 5999

XVFB_BIN = "Xvfb"
X11VNC_BIN = "x11vnc"

STARTUP_POLL_SECONDS = 0.2
XVFB_STARTUP_TIMEOUT_SECONDS = 5.0
PORT_STARTUP_TIMEOUT_SECONDS = 5.0

# apt package name for a binary this module shells out to, so a missing
# binary can be reported as an actionable install command instead of a bare
# FileNotFoundError.
_APT_PACKAGE = {"Xvfb": "xvfb", "x11vnc": "x11vnc"}

# Serializes start_all and stop_all, so two dashboard requests (or a request
# and the login CLI's background thread) never spawn a second Xvfb or kill a
# leg the other call is still starting.
_STACK_LOCK = threading.Lock()


class DisplayStackError(RuntimeError):
    """A leg of the stack (Xvfb or x11vnc) could not be started."""


@dataclass(frozen=True)
class DisplayConfig:
    display: str
    geometry: str
    x11vnc_port: int
    run_dir: Path

    @property
    def vnc_password_path(self) -> Path:
        return self.run_dir / "vnc-password"

    @property
    def xvfb_pid_path(self) -> Path:
        return self.run_dir / "xvfb.pid"

    @property
    def x11vnc_pid_path(self) -> Path:
        return self.run_dir / "x11vnc.pid"

    @property
    def xvfb_log_path(self) -> Path:
        return self.run_dir / "xvfb.log"

    @property
    def x11vnc_log_path(self) -> Path:
        return self.run_dir / "x11vnc.log"


def load_display_config(config: Config) -> DisplayConfig:
    """Reads the display, ports, and run directory from the environment (so
    tests and multiple machines can use different values), defaulting to
    `:99` and 5999 (x11vnc)."""
    display = os.environ.get("AUTOJOB_VNC_DISPLAY", DEFAULT_DISPLAY)
    geometry = os.environ.get("AUTOJOB_VNC_GEOMETRY", DEFAULT_GEOMETRY)
    x11vnc_port = int(os.environ.get("AUTOJOB_VNC_PORT", DEFAULT_X11VNC_PORT))
    return DisplayConfig(
        display=display,
        geometry=geometry,
        x11vnc_port=x11vnc_port,
        run_dir=config.data_dir / "vnc",
    )


def ensure_vnc_password(display_config: DisplayConfig) -> str:
    """Returns the x11vnc password, generating and storing it 0600 on first
    use. Never printed, logged, or put on a command line; the `/browser` page
    reads it straight from this file to hand to the noVNC client in an
    authenticated session, and x11vnc reads the same file through
    `-passwdfile`."""
    display_config.run_dir.mkdir(parents=True, exist_ok=True)
    path = display_config.vnc_password_path
    if path.exists():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return path.read_text().strip()
    password = secrets.token_urlsafe(18)
    # Created 0600 from the start, so it is never readable by others even
    # for an instant.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        handle.write(password)
    return password


def _proc_state(pid: int) -> str | None:
    """The one-letter state from `/proc/<pid>/stat` (`R`, `S`, `Z`, ...), or
    None if the process is gone. The command name in parentheses may itself
    contain spaces or parentheses, so the state is read after the last `)`."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    rest = raw.rpartition(")")[2].split()
    return rest[0] if rest else None


def _pid_alive(pid: int) -> bool:
    """True for a running process. A zombie (exited but not yet reaped)
    counts as dead; if it is our own child it is reaped here so it does not
    linger."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, just owned by someone else
    state = _proc_state(pid)
    if state is None:
        return False
    if state == "Z":
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass  # not our child; its own parent has to reap it
        return False
    return True


def _pid_runs(pid: int, binary: str) -> bool:
    """Whether `pid` is running `binary`, by its argv[0] in
    `/proc/<pid>/cmdline`. Guards against a pid file that survived a reboot
    and now points at an unrelated process."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    argv0 = raw.split(b"\0", 1)[0].decode(errors="replace")
    return os.path.basename(argv0) == binary


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _leg_running(pid_path: Path, binary: str) -> bool:
    pid = _read_pid(pid_path)
    return pid is not None and _pid_alive(pid) and _pid_runs(pid, binary)


def status(config: Config) -> dict:
    """Whether each leg of the stack is up, by pid file, plus the display and
    x11vnc port the `/browser` page needs to know regardless. Handles "not
    running" for a fresh data dir the same as a stopped stack: no pid files
    means every leg reads False."""
    display_config = load_display_config(config)
    xvfb_up = _leg_running(display_config.xvfb_pid_path, XVFB_BIN)
    x11vnc_up = _leg_running(display_config.x11vnc_pid_path, X11VNC_BIN)
    return {
        "xvfb": xvfb_up,
        "x11vnc": x11vnc_up,
        "all_running": xvfb_up and x11vnc_up,
        "display": display_config.display,
        "x11vnc_port": display_config.x11vnc_port,
    }


def _wait_for_x11_socket(display: str, timeout: float) -> bool:
    number = display.lstrip(":").split(".")[0]
    socket_path = Path(f"/tmp/.X11-unix/X{number}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if socket_path.exists():
            return True
        time.sleep(STARTUP_POLL_SECONDS)
    return socket_path.exists()


STOP_WAIT_SECONDS = 2.0


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(STARTUP_POLL_SECONDS)
    return False


def missing_binaries() -> list[str]:
    """Names of the stack's binaries that are not on PATH."""
    return [name for name in (XVFB_BIN, X11VNC_BIN) if shutil.which(name) is None]


def _missing_binary_error(binary: str) -> DisplayStackError:
    package = _APT_PACKAGE.get(binary, binary)
    return DisplayStackError(
        f"{binary} is not installed. Install it with:\n  sudo apt-get install -y {package}"
    )


def _spawn(cmd: list[str], pid_path: Path, log_path: Path) -> int:
    """Starts `cmd` detached (its own session, so it outlives this request),
    appends its output to `log_path`, and records its pid at `pid_path`.
    Raises `DisplayStackError` with an apt-get hint if the binary itself is
    missing, instead of a bare `FileNotFoundError`."""
    try:
        log_file = open(log_path, "ab")
    except OSError as exc:
        raise DisplayStackError(f"Could not open log file {log_path}: {exc}") from exc
    try:
        process = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise _missing_binary_error(cmd[0]) from exc
    finally:
        log_file.close()
    pid_path.write_text(str(process.pid))
    return process.pid


def _start_xvfb(display_config: DisplayConfig) -> None:
    cmd = [XVFB_BIN, display_config.display, "-screen", "0", display_config.geometry, "-nolisten", "tcp"]
    pid = _spawn(cmd, display_config.xvfb_pid_path, display_config.xvfb_log_path)
    # A socket alone can be stale or belong to a foreign server on the same
    # display, so the Xvfb we just spawned must also still be alive.
    if not (
        _wait_for_x11_socket(display_config.display, XVFB_STARTUP_TIMEOUT_SECONDS)
        and _pid_alive(pid)
    ):
        _stop_leg(display_config.xvfb_pid_path, XVFB_BIN)
        raise DisplayStackError(
            f"Xvfb did not open display {display_config.display} within "
            f"{XVFB_STARTUP_TIMEOUT_SECONDS:g}s. See {display_config.xvfb_log_path}"
        )


def _start_x11vnc(display_config: DisplayConfig) -> None:
    # `-passwdfile` reads the plain password from our 0600 file, so it never
    # appears on a command line (`-storepasswd <pass>` or `-passwd <pass>`
    # would expose it in `ps` to every local user).
    cmd = [
        X11VNC_BIN,
        "-display", display_config.display,
        "-rfbport", str(display_config.x11vnc_port),
        "-passwdfile", str(display_config.vnc_password_path),
        "-localhost",
        "-forever",
        "-shared",
        "-noxdamage",
        "-quiet",
    ]
    _spawn(cmd, display_config.x11vnc_pid_path, display_config.x11vnc_log_path)
    if not _wait_for_port("127.0.0.1", display_config.x11vnc_port, PORT_STARTUP_TIMEOUT_SECONDS):
        _stop_leg(display_config.x11vnc_pid_path, X11VNC_BIN)
        raise DisplayStackError(
            f"x11vnc did not listen on 127.0.0.1:{display_config.x11vnc_port} within "
            f"{PORT_STARTUP_TIMEOUT_SECONDS:g}s. See {display_config.x11vnc_log_path}"
        )


def start_all(config: Config) -> dict:
    """Idempotently starts every leg not already running (by pid file), in
    dependency order (Xvfb, then x11vnc against it), and returns the same
    shape as `status()`. Raises `DisplayStackError` if a binary is missing or
    a leg does not come up in time. The failed leg is stopped and its pid
    file cleared; a leg that already started (Xvfb, when x11vnc fails) is
    left running rather than torn back down."""
    with _STACK_LOCK:
        display_config = load_display_config(config)
        display_config.run_dir.mkdir(parents=True, exist_ok=True)
        ensure_vnc_password(display_config)

        if not _leg_running(display_config.xvfb_pid_path, XVFB_BIN):
            _start_xvfb(display_config)
        if not _leg_running(display_config.x11vnc_pid_path, X11VNC_BIN):
            _start_x11vnc(display_config)
        return status(config)


def _stop_leg(pid_path: Path, binary: str) -> None:
    """SIGTERMs the leg's process only if it is alive and still running
    `binary`, then clears the pid file either way."""
    pid = _read_pid(pid_path)
    if pid is not None and _pid_alive(pid) and _pid_runs(pid, binary):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + STOP_WAIT_SECONDS
        while _pid_alive(pid) and time.time() < deadline:
            time.sleep(STARTUP_POLL_SECONDS)
    pid_path.unlink(missing_ok=True)


def stop_all(config: Config) -> dict:
    """Stops every leg (reverse order: x11vnc, then Xvfb) and
    clears their pid files. A leg with no pid file, or a stale one, is
    silently skipped: stopping an already-stopped stack is not an error."""
    with _STACK_LOCK:
        display_config = load_display_config(config)
        _stop_leg(display_config.x11vnc_pid_path, X11VNC_BIN)
        _stop_leg(display_config.xvfb_pid_path, XVFB_BIN)
        return status(config)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in ("start", "stop", "status"):
        print("Usage: python -m autojob.browser.display <start|stop|status>")
        return 1
    from autojob.config import load_config

    config = load_config()
    action = argv[0]
    try:
        if action == "start":
            result = start_all(config)
        elif action == "stop":
            result = stop_all(config)
        else:
            result = status(config)
    except DisplayStackError as exc:
        print(str(exc))
        return 1
    for key in ("xvfb", "x11vnc"):
        print(f"{key}: {'up' if result[key] else 'down'}")
    print(f"display: {result['display']}  x11vnc port: {result['x11vnc_port']}")
    if action == "start" and not result["all_running"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

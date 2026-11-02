"""Tests for the Xvfb + x11vnc display stack manager. No real process starts:
every spawn, wait, and password-store call is faked."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import threading
import time

import pytest

from autojob.browser import display
from autojob.browser.display import DisplayStackError


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("AUTOJOB_VNC_DISPLAY", "AUTOJOB_VNC_GEOMETRY", "AUTOJOB_VNC_PORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fakes(monkeypatch):
    spawned: list[list[str]] = []

    def fake_spawn(cmd, pid_path, log_path):
        spawned.append(list(cmd))
        pid_path.write_text("4242")
        return 4242

    monkeypatch.setattr(display, "_spawn", fake_spawn)
    monkeypatch.setattr(display, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(display, "_pid_runs", lambda pid, binary: True)
    monkeypatch.setattr(display, "_wait_for_x11_socket", lambda *a, **k: True)
    monkeypatch.setattr(display, "_wait_for_port", lambda *a, **k: True)
    monkeypatch.setattr(display, "STOP_WAIT_SECONDS", 0.05)
    return {"spawned": spawned}


def test_status_on_fresh_data_dir(config, fakes):
    result = display.status(config)
    assert result["xvfb"] is False
    assert result["x11vnc"] is False
    assert result["all_running"] is False
    assert result["x11vnc_port"] == 5999


def test_start_all_spawns_in_order_and_is_idempotent(config, fakes):
    result = display.start_all(config)
    spawned = fakes["spawned"]
    assert [cmd[0] for cmd in spawned] == ["Xvfb", "x11vnc"]
    xvfb, x11vnc = spawned
    assert display.DEFAULT_DISPLAY in xvfb
    assert xvfb[xvfb.index("-nolisten") + 1] == "tcp"
    assert "-localhost" in x11vnc
    assert x11vnc[x11vnc.index("-rfbport") + 1] == "5999"
    display_config = display.load_display_config(config)
    assert x11vnc[x11vnc.index("-passwdfile") + 1] == str(display_config.vnc_password_path)
    password = display_config.vnc_password_path.read_text()
    assert not any(password in arg for cmd in spawned for arg in cmd)
    assert result["all_running"] is True

    display.start_all(config)
    assert len(fakes["spawned"]) == 2


def test_stale_pid_file_is_respawned(config, fakes, monkeypatch):
    display_config = display.load_display_config(config)
    display_config.run_dir.mkdir(parents=True)
    display_config.xvfb_pid_path.write_text("4242")
    spawned_flag = fakes["spawned"]
    monkeypatch.setattr(display, "_pid_alive", lambda pid: bool(spawned_flag))
    display.start_all(config)
    assert [cmd[0] for cmd in fakes["spawned"]] == ["Xvfb", "x11vnc"]


def test_x11vnc_missing_leaves_xvfb_up(config, fakes, monkeypatch):
    original = display._spawn

    def spawn(cmd, pid_path, log_path):
        if cmd[0] == "x11vnc":
            raise DisplayStackError("x11vnc is not installed")
        return original(cmd, pid_path, log_path)

    monkeypatch.setattr(display, "_spawn", spawn)
    with pytest.raises(DisplayStackError):
        display.start_all(config)
    result = display.status(config)
    assert result["xvfb"] is True
    assert result["x11vnc"] is False


def test_xvfb_socket_timeout_raises_and_cleans_up(config, fakes, monkeypatch):
    kills = []
    monkeypatch.setattr(display, "_wait_for_x11_socket", lambda *a, **k: False)
    monkeypatch.setattr(display.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    with pytest.raises(DisplayStackError, match="Xvfb did not open display"):
        display.start_all(config)
    assert [cmd[0] for cmd in fakes["spawned"]] == ["Xvfb"]
    assert kills == [(4242, signal.SIGTERM)]
    assert not display.load_display_config(config).xvfb_pid_path.exists()


def test_x11vnc_port_timeout_raises_and_cleans_up(config, fakes, monkeypatch):
    kills = []
    monkeypatch.setattr(display, "_wait_for_port", lambda *a, **k: False)
    monkeypatch.setattr(display.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    with pytest.raises(DisplayStackError, match="x11vnc did not listen"):
        display.start_all(config)
    display_config = display.load_display_config(config)
    assert kills == [(4242, signal.SIGTERM)]
    assert not display_config.x11vnc_pid_path.exists()
    assert display_config.xvfb_pid_path.exists()


def test_reused_pid_is_not_ours(config, fakes, monkeypatch):
    display_config = display.load_display_config(config)
    display_config.run_dir.mkdir(parents=True)
    display_config.xvfb_pid_path.write_text("4242")
    display_config.x11vnc_pid_path.write_text("4242")
    monkeypatch.setattr(display, "_pid_runs", lambda pid, binary: False)
    assert display.status(config)["xvfb"] is False

    kills = []
    monkeypatch.setattr(display.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    display.stop_all(config)
    assert kills == []
    assert not display_config.xvfb_pid_path.exists()

    display_config.xvfb_pid_path.write_text("4242")
    display.start_all(config)
    assert [cmd[0] for cmd in fakes["spawned"]] == ["Xvfb", "x11vnc"]


def test_start_all_is_serialized(config, fakes, monkeypatch):
    original = display._spawn
    entered, release = threading.Event(), threading.Event()

    def slow_spawn(cmd, pid_path, log_path):
        if cmd[0] == "Xvfb":
            entered.set()
            release.wait(5)
        return original(cmd, pid_path, log_path)

    monkeypatch.setattr(display, "_spawn", slow_spawn)
    first = threading.Thread(target=display.start_all, args=(config,))
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=display.start_all, args=(config,))
    second.start()
    time.sleep(0.1)
    # The second call is blocked on the lock, not spawning its own Xvfb.
    assert second.is_alive()
    release.set()
    first.join(5)
    second.join(5)
    assert [cmd[0] for cmd in fakes["spawned"]] == ["Xvfb", "x11vnc"]


def test_stop_all_signals_x11vnc_then_xvfb(config, fakes, monkeypatch):
    display.start_all(config)
    display_config = display.load_display_config(config)
    # Distinct pids so the order is observable.
    display_config.x11vnc_pid_path.write_text("4242")
    kills = []
    monkeypatch.setattr(display.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    display_config.xvfb_pid_path.write_text("4242")
    display.stop_all(config)
    assert kills == [(4242, signal.SIGTERM), (4242, signal.SIGTERM)]
    assert not display_config.xvfb_pid_path.exists()
    assert not display_config.x11vnc_pid_path.exists()


def test_stop_all_order(config, fakes, monkeypatch):
    display_config = display.load_display_config(config)
    display_config.run_dir.mkdir(parents=True)
    display_config.xvfb_pid_path.write_text("4242")
    display_config.x11vnc_pid_path.write_text("4242")
    order = []
    original = display._stop_leg
    monkeypatch.setattr(display, "_stop_leg", lambda path, binary: (order.append(path.name), original(path, binary)))
    monkeypatch.setattr(display.os, "kill", lambda pid, sig: None)
    display.stop_all(config)
    assert order == ["x11vnc.pid", "xvfb.pid"]


def test_stop_all_with_no_pid_files_kills_nothing(config, fakes, monkeypatch):
    kills = []
    monkeypatch.setattr(display.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    display.stop_all(config)
    assert kills == []


def test_proc_state_parses_comm_with_parens(monkeypatch):
    monkeypatch.setattr(
        display.Path, "read_text", lambda self: "123 (we) ird (x) Z 1 2 3\n"
    )
    assert display._proc_state(123) == "Z"


def test_zombie_child_is_dead_and_reaped():
    child = subprocess.Popen(["true"])
    deadline = time.time() + 5
    while display._proc_state(child.pid) != "Z" and time.time() < deadline:
        time.sleep(0.02)
    assert display._proc_state(child.pid) == "Z"
    assert display._pid_alive(child.pid) is False
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
    child.returncode = 0


def test_pid_runs_checks_cmdline():
    child = subprocess.Popen(["sleep", "30"])
    try:
        assert display._pid_alive(child.pid) is True
        assert display._pid_runs(child.pid, "sleep") is True
        assert display._pid_runs(child.pid, "Xvfb") is False
    finally:
        child.kill()
        child.wait()


def test_ensure_vnc_password_is_private_and_stable(config):
    display_config = display.load_display_config(config)
    first = display.ensure_vnc_password(display_config)
    second = display.ensure_vnc_password(display_config)
    assert first and first == second
    mode = stat.S_IMODE(display_config.vnc_password_path.stat().st_mode)
    assert mode == 0o600


def test_missing_binaries(monkeypatch):
    monkeypatch.setattr(display.shutil, "which", lambda name: None if name == "x11vnc" else "/usr/bin/" + name)
    assert display.missing_binaries() == ["x11vnc"]


def test_missing_binary_error_has_apt_hint():
    assert "sudo apt-get install -y x11vnc" in str(display._missing_binary_error("x11vnc"))


def test_spawn_missing_binary_raises_and_writes_no_pid(tmp_path):
    pid_path = tmp_path / "x.pid"
    with pytest.raises(DisplayStackError, match="is not installed"):
        display._spawn(["autojob-no-such-binary-xyz"], pid_path, tmp_path / "x.log")
    assert not pid_path.exists()


def test_main_status_prints_both_legs(config, capsys, monkeypatch):
    monkeypatch.setenv("AUTOJOB_DATA_DIR", str(config.data_dir))
    assert display.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "xvfb: down" in out
    assert "x11vnc: down" in out


# --- context and login ---

def test_visible_launch_env_sets_display(config, monkeypatch):
    from autojob.browser.context import visible_launch_env

    monkeypatch.setenv("AUTOJOB_VNC_DISPLAY", ":77")
    env = visible_launch_env(config)
    assert env["DISPLAY"] == ":77"
    assert "PATH" in env


def test_login_main_success_points_at_browser_page(config, monkeypatch, capsys):
    from autojob.browser import login

    ran = []
    monkeypatch.setattr(login, "start_all", lambda cfg: {"all_running": True})
    monkeypatch.setattr(login, "run_login_session", lambda cfg, site: ran.append(site))
    assert login.main(["linkedin"]) == 0
    assert ran == ["linkedin"]
    assert "/browser" in capsys.readouterr().out


def test_login_main_reports_display_failure(config, monkeypatch, capsys):
    from autojob.browser import login

    def boom(cfg):
        raise DisplayStackError("x11vnc is not installed")

    monkeypatch.setattr(login, "start_all", boom)
    assert login.main(["linkedin"]) == 1
    assert "x11vnc is not installed" in capsys.readouterr().out


def test_login_main_holds_run_lock_and_refuses_during_run(config, monkeypatch, capsys):
    import fcntl

    from autojob.browser import login
    from autojob.pipeline.lock import run_active

    held = []
    monkeypatch.setattr(login, "start_all", lambda cfg: {"all_running": True})
    monkeypatch.setattr(login, "run_login_session", lambda cfg, site: held.append(run_active(config)))
    assert login.main(["linkedin"]) == 0
    assert held == [True]

    config.data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(config.run_lock_path, "a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert login.main(["linkedin"]) == 1
    finally:
        handle.close()
    assert held == [True]
    assert "A run is in progress" in capsys.readouterr().out


def test_login_main_unknown_site(config):
    from autojob.browser import login

    assert login.main(["nope"]) == 1


def test_run_login_session_opens_login_page_and_returns(config, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from autojob.browser import login
    from autojob.browser.context import LOGIN_URLS

    gotos = []
    page = SimpleNamespace(
        goto=lambda url, wait_until=None: gotos.append((url, wait_until)),
        context=SimpleNamespace(pages=[]),
    )

    @contextmanager
    def fake_page(cfg, **kwargs):
        yield page

    monkeypatch.setattr(login, "browser_page", fake_page)
    monkeypatch.setattr(login, "POLL_SECONDS", 0)
    login.run_login_session(config, "linkedin")
    assert gotos == [(LOGIN_URLS["linkedin"], "domcontentloaded")]


def test_xvfb_dead_after_socket_wait_is_failure(config, fakes, monkeypatch):
    monkeypatch.setattr(display, "_pid_alive", lambda pid: False)
    with pytest.raises(DisplayStackError):
        display.start_all(config)
    display_config = display.load_display_config(config)
    assert not display_config.xvfb_pid_path.exists()


def test_stop_leg_waits_for_exit_before_unlinking(config, fakes, monkeypatch):
    display_config = display.load_display_config(config)
    display_config.run_dir.mkdir(parents=True)
    display_config.xvfb_pid_path.write_text("4242")
    monkeypatch.setattr(display.os, "kill", lambda pid, sig: None)
    state = {"polls": 0}

    def alive(pid):
        state["polls"] += 1
        return state["polls"] < 4

    monkeypatch.setattr(display, "_pid_alive", alive)
    monkeypatch.setattr(display, "STOP_WAIT_SECONDS", 2.0)
    display._stop_leg(display_config.xvfb_pid_path, display.XVFB_BIN)
    assert state["polls"] >= 4
    assert not display_config.xvfb_pid_path.exists()

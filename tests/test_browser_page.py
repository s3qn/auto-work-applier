from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from autojob import db
from autojob.web import routes
from autojob.web.deps import get_browser_visible_mode
from tests.conftest import get_csrf_token, login

RUNNING = {"xvfb": True, "x11vnc": True, "all_running": True, "display": ":99", "x11vnc_port": 5999}
STOPPED = {"xvfb": False, "x11vnc": False, "all_running": False, "display": ":99", "x11vnc_port": 5999}


def test_logged_out_redirects(client, with_password):
    response = client.get("/browser", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_page_shows_install_hint_when_missing(client, with_password, monkeypatch):
    login(client, with_password)
    monkeypatch.setattr(routes.display, "missing_binaries", lambda: ["x11vnc"])
    monkeypatch.setattr(routes.display, "status", lambda config: STOPPED)
    response = client.get("/browser")
    assert response.status_code == 200
    assert "sudo apt-get install -y x11vnc" in response.text
    assert "rfb.js" not in response.text


def test_page_renders_viewer_when_running(client, with_password, monkeypatch):
    login(client, with_password)
    monkeypatch.setattr(routes.display, "missing_binaries", lambda: [])
    monkeypatch.setattr(routes.display, "status", lambda config: RUNNING)
    monkeypatch.setattr(routes.display, "ensure_vnc_password", lambda dc: "pw-test")
    text = client.get("/browser").text
    assert "/static/novnc/core/rfb.js" in text
    assert '"pw-test"' in text
    assert "/browser/ws" in text
    assert "cdn" not in text.lower()


def test_start_and_notice_shown_once(client, with_password, monkeypatch):
    login(client, with_password)
    token = get_csrf_token(client, "/browser")
    monkeypatch.setattr(routes.display, "start_all", lambda config: RUNNING)
    response = client.post("/browser/start", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/browser"

    def boom(config):
        raise routes.display.DisplayStackError("x11vnc is not installed. Install it.")

    monkeypatch.setattr(routes.display, "start_all", boom)
    client.post("/browser/start", data={"csrf_token": token}, follow_redirects=False)
    assert "x11vnc is not installed. Install it." in client.get("/browser").text
    assert "x11vnc is not installed. Install it." not in client.get("/browser").text


def test_stop_calls_stop_all(client, with_password, monkeypatch):
    login(client, with_password)
    token = get_csrf_token(client, "/browser")
    calls = []
    monkeypatch.setattr(routes.display, "stop_all", lambda config: calls.append(1) or STOPPED)
    response = client.post("/browser/stop", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert calls == [1]


def test_stop_refused_while_browser_busy(client, with_password, monkeypatch):
    login(client, with_password)
    token = get_csrf_token(client, "/browser")
    calls = []
    monkeypatch.setattr(routes.display, "stop_all", lambda config: calls.append(1) or STOPPED)
    monkeypatch.setattr(routes, "_login_alive", lambda app: True)
    response = client.post("/browser/stop", data={"csrf_token": token}, follow_redirects=False)
    assert response.headers["location"] == "/browser?busy=1"
    monkeypatch.setattr(routes, "_login_alive", lambda app: False)
    assert routes._BROWSER_LOCK.acquire(blocking=False)
    try:
        response = client.post("/browser/stop", data={"csrf_token": token}, follow_redirects=False)
    finally:
        routes._BROWSER_LOCK.release()
    assert response.headers["location"] == "/browser?busy=1"
    assert calls == []


def test_settings_toggle(client, with_password, config):
    login(client, with_password)
    token = get_csrf_token(client, "/browser")
    response = client.post(
        "/browser/settings", data={"csrf_token": token, "browser_visible_mode": "true"}, follow_redirects=False
    )
    assert response.headers["location"] == "/browser?saved=1"
    conn = db.get_connection(config)
    try:
        assert get_browser_visible_mode(conn) is True
    finally:
        conn.close()
    assert 'name="browser_visible_mode" value="true" checked' in client.get("/browser").text
    client.post("/browser/settings", data={"csrf_token": token}, follow_redirects=False)
    conn = db.get_connection(config)
    try:
        assert get_browser_visible_mode(conn) is False
    finally:
        conn.close()


def test_login_starts_session(client, app, with_password, monkeypatch):
    login(client, with_password)
    token = get_csrf_token(client, "/browser")
    started, sites = [], []
    monkeypatch.setattr(routes.display, "start_all", lambda config: started.append(1) or RUNNING)
    monkeypatch.setattr(routes.login, "run_login_session", lambda config, site: sites.append(site))
    response = client.post("/browser/login/linkedin", data={"csrf_token": token}, follow_redirects=False)
    assert response.headers["location"] == "/browser?login=started"
    app.state.login_thread.join(timeout=5)
    assert sites == ["linkedin"]
    assert client.post("/browser/login/nope", data={"csrf_token": token}).status_code == 404


def _hold_flock(config):
    import fcntl

    config.data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(config.run_lock_path, "a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_login_session_holds_run_lock(client, app, with_password, config, monkeypatch):
    from autojob.pipeline.lock import run_active

    login(client, with_password)
    token = get_csrf_token(client, "/browser")
    held = []
    monkeypatch.setattr(routes.display, "start_all", lambda config: RUNNING)
    monkeypatch.setattr(routes.login, "run_login_session", lambda cfg, site: held.append(run_active(config)))
    client.post("/browser/login/alljobs", data={"csrf_token": token}, follow_redirects=False)
    app.state.login_thread.join(timeout=5)
    assert held == [True]
    assert run_active(config) is False


def test_login_redirects_busy_while_run_active(client, app, with_password, config, monkeypatch):
    login(client, with_password)
    token = get_csrf_token(client, "/browser")
    sites = []
    monkeypatch.setattr(routes.display, "start_all", lambda config: RUNNING)
    monkeypatch.setattr(routes.login, "run_login_session", lambda cfg, site: sites.append(site))
    handle = _hold_flock(config)
    try:
        response = client.post("/browser/login/linkedin", data={"csrf_token": token}, follow_redirects=False)
    finally:
        handle.close()
    assert response.headers["location"] == "/browser?busy=1"
    assert sites == []


def test_login_thread_reports_run_in_progress(config, monkeypatch):
    from types import SimpleNamespace

    fake_app = SimpleNamespace(state=SimpleNamespace(browser_notice=None))
    sites = []
    monkeypatch.setattr(routes.login, "run_login_session", lambda cfg, site: sites.append(site))
    handle = _hold_flock(config)
    try:
        routes._run_login_session_in_background(fake_app, config, "linkedin")
    finally:
        handle.close()
    assert sites == []
    assert fake_app.state.browser_notice == "A run is in progress; try the login after it finishes."


def test_stop_refused_while_run_active(client, with_password, config, monkeypatch):
    login(client, with_password)
    token = get_csrf_token(client, "/browser")
    calls = []
    monkeypatch.setattr(routes.display, "stop_all", lambda config: calls.append(1) or STOPPED)
    handle = _hold_flock(config)
    try:
        response = client.post("/browser/stop", data={"csrf_token": token}, follow_redirects=False)
    finally:
        handle.close()
    assert response.headers["location"] == "/browser?busy=1"
    assert calls == []


def test_post_without_csrf_is_403(client, with_password):
    login(client, with_password)
    assert client.post("/browser/stop", data={"csrf_token": "bad"}).status_code == 403


def test_static_novnc_served(client, with_password):
    assert client.get("/static/novnc/core/rfb.js").status_code == 200
    assert client.get("/static/novnc/LICENSE.txt").status_code == 200


# --- websocket ---------------------------------------------------------------


ORIGIN = {"Origin": "http://testserver"}


class FakeWriter:
    """Records what the relay writes to x11vnc. Once given a reader, feeds
    it EOF on the first write, so the relay ends on its own (x11vnc
    hanging up) instead of being cancelled when the test client leaves."""

    def __init__(self):
        self.written = []
        self.reader = None

    def write(self, data):
        self.written.append(data)
        if self.reader is not None:
            self.reader.feed_eof()

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


def test_ws_logged_out_rejected(client, with_password, monkeypatch):
    called = []

    async def fake_open(host, port):
        called.append(1)
        raise OSError

    monkeypatch.setattr(routes, "_open_vnc_connection", fake_open)
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/browser/ws"):
            pass
    assert info.value.code == 1008
    assert called == []


def test_ws_connection_refused(client, with_password, monkeypatch):
    login(client, with_password)

    async def fake_open(host, port):
        raise OSError("refused")

    monkeypatch.setattr(routes, "_open_vnc_connection", fake_open)
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/browser/ws", headers=ORIGIN):
            pass
    assert info.value.code == 1011


def _relay_once(client, monkeypatch, headers):
    writer = FakeWriter()

    async def fake_open(host, port):
        reader = asyncio.StreamReader()
        reader.feed_data(b"RFB 003.008\n")
        writer.reader = reader
        return reader, writer

    monkeypatch.setattr(routes, "_open_vnc_connection", fake_open)
    with client.websocket_connect("/browser/ws", headers=headers) as ws:
        ws.send_bytes(b"hi")
        assert ws.receive_bytes() == b"RFB 003.008\n"
        # The fake x11vnc hangs up after the write; drain the server's close
        # here so nothing is still running when the block exits.
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    assert writer.written == [b"hi"]


def test_ws_relays_bytes(client, with_password, monkeypatch):
    login(client, with_password)
    _relay_once(client, monkeypatch, ORIGIN)


def test_ws_public_host_origin_allowed(client, with_password, monkeypatch):
    # cloudflared normally forwards Host unchanged; the allowlist covers a
    # proxy that rewrites it to the local address.
    login(client, with_password)
    monkeypatch.delenv("AUTOJOB_PUBLIC_HOST", raising=False)
    _relay_once(client, monkeypatch, {"Origin": "https://autojob.sean.build"})


def test_ws_public_host_is_configurable(client, with_password, monkeypatch):
    login(client, with_password)
    monkeypatch.setenv("AUTOJOB_PUBLIC_HOST", "jobs.example.org, other.example")
    _relay_once(client, monkeypatch, {"Origin": "https://jobs.example.org"})

    async def fake_open(host, port):
        raise AssertionError("must not connect")

    monkeypatch.setattr(routes, "_open_vnc_connection", fake_open)
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/browser/ws", headers={"Origin": "https://autojob.sean.build"}):
            pass
    assert info.value.code == 1008


def test_ws_missing_origin_rejected(client, with_password, monkeypatch):
    login(client, with_password)

    async def fake_open(host, port):
        raise AssertionError("must not connect")

    monkeypatch.setattr(routes, "_open_vnc_connection", fake_open)
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/browser/ws"):
            pass
    assert info.value.code == 1008


def test_ws_bad_origin_rejected(client, with_password, monkeypatch):
    login(client, with_password)

    async def fake_open(host, port):
        raise AssertionError("must not connect")

    monkeypatch.setattr(routes, "_open_vnc_connection", fake_open)
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/browser/ws", headers={"Origin": "https://evil.example"}):
            pass
    assert info.value.code == 1008

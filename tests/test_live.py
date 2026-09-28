"""Tests for the live SSE view: the run_events JSON polling fallback, the
live terminal and progress panel pages, and the SSE generator itself. No
real waiting: every test either uses a run already marked finished or
exercises route behavior directly, never sits through the SSE loop's
~500ms poll."""

from __future__ import annotations

import asyncio

from tests.conftest import login


def _seed_run_with_events(config):
    from autojob import db
    from autojob.pipeline import events

    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        events.emit(conn, run_id, "system", "info", "Run started (manual).")
        events.emit(
            conn, run_id, "filter", "score",
            "score 7 keep - Penetration Tester @ Kestrel Ops: junior-friendly, matches CPTS",
            detail={"status": "queued", "fit_score": 7.0},
        )
        events.bump_progress(conn, run_id, "filter", done=1, total=3, filtered=0)
    finally:
        conn.close()
    return run_id


# --- /runs/{id}/events (JSON polling fallback) -----------------------------

def test_run_events_json_returns_new_rows_in_order(client, with_password, config):
    login(client, with_password)
    run_id = _seed_run_with_events(config)

    response = client.get(f"/runs/{run_id}/events?since=0")
    assert response.status_code == 200
    payload = response.json()
    assert [e["kind"] for e in payload["events"]] == ["info", "score", "progress"]
    assert payload["finished"] is False

    first_id = payload["events"][0]["id"]
    response2 = client.get(f"/runs/{run_id}/events?since={first_id}")
    assert [e["kind"] for e in response2.json()["events"]] == ["score", "progress"]


def test_run_events_json_requires_login(client, config):
    run_id = _seed_run_with_events(config)
    response = client.get(f"/runs/{run_id}/events?since=0", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_run_events_json_404_for_missing_run(client, with_password):
    login(client, with_password)
    response = client.get("/runs/999/events?since=0")
    assert response.status_code == 404


# --- live and progress pages -----------------------------------------------

def test_live_page_renders_when_logged_in(client, with_password, config):
    login(client, with_password)
    run_id = _seed_run_with_events(config)
    response = client.get(f"/runs/{run_id}/live")
    assert response.status_code == 200
    assert "term-output" in response.text
    assert "Pause auto-scroll" in response.text


def test_live_page_requires_login(client, config):
    run_id = _seed_run_with_events(config)
    response = client.get(f"/runs/{run_id}/live", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_live_page_404_for_missing_run(client, with_password):
    login(client, with_password)
    response = client.get("/runs/999/live")
    assert response.status_code == 404


def test_runs_live_redirects_to_the_latest_run(client, with_password, config):
    login(client, with_password)
    from autojob import db

    conn = db.get_connection(config)
    try:
        db.create_run(conn, trigger="manual")
        latest_id = db.create_run(conn, trigger="manual")
    finally:
        conn.close()

    response = client.get("/runs/live", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/runs/{latest_id}/live"


def test_runs_live_redirects_to_run_log_when_no_runs_yet(client, with_password):
    login(client, with_password)
    response = client.get("/runs/live", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/runs"


def test_run_detail_page_shows_progress_bars_and_a_live_link(client, with_password, config):
    login(client, with_password)
    run_id = _seed_run_with_events(config)
    response = client.get(f"/runs/{run_id}")
    assert response.status_code == 200
    assert "progress-bar" in response.text
    assert f"/runs/{run_id}/live" in response.text


def test_run_progress_partial_renders_and_requires_login(client, with_password, config):
    run_id = _seed_run_with_events(config)

    anon = client.get(f"/runs/{run_id}/progress", follow_redirects=False)
    assert anon.status_code == 303

    login(client, with_password)
    response = client.get(f"/runs/{run_id}/progress")
    assert response.status_code == 200
    assert "1/3" in response.text  # filter progress bumped to done=1, total=3


# --- the SSE stream endpoint itself (login-gated) --------------------------

def test_run_events_stream_requires_login(client, config):
    run_id = _seed_run_with_events(config)
    response = client.get(f"/runs/{run_id}/stream", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- the SSE generator directly, no HTTP, no real waiting ------------------

def test_stream_run_events_replays_then_emits_finished_for_a_finished_run(config):
    from autojob import db
    from autojob.web.routes import stream_run_events

    run_id = _seed_run_with_events(config)
    conn = db.get_connection(config)
    try:
        db.finish_run(conn, run_id, found=1, filtered=0, applied=0, failed=0, log="done")
    finally:
        conn.close()

    async def collect():
        return [frame async for frame in stream_run_events(config, run_id, since_id=0)]

    frames = asyncio.run(collect())
    assert len(frames) == 4  # 3 replayed events + 1 synthetic "finished" frame
    assert '"kind": "info"' in frames[0]
    assert '"kind": "score"' in frames[1]
    assert '"kind": "progress"' in frames[2]
    assert '"kind": "finished"' in frames[-1]
    assert 'Run finished.' in frames[-1]


def test_stream_run_events_honors_since_id_on_replay(config):
    from autojob import db
    from autojob.web.routes import stream_run_events

    run_id = _seed_run_with_events(config)
    conn = db.get_connection(config)
    try:
        first_id = db.list_run_events_since(conn, run_id, 0)[0]["id"]
        db.finish_run(conn, run_id, found=1, filtered=0, applied=0, failed=0, log="done")
    finally:
        conn.close()

    async def collect():
        return [frame async for frame in stream_run_events(config, run_id, since_id=first_id)]

    frames = asyncio.run(collect())
    assert len(frames) == 3  # the remaining 2 events + the finished frame
    assert '"kind": "score"' in frames[0]


def test_stream_run_events_stops_immediately_if_the_run_does_not_exist(config):
    from autojob import db
    from autojob.web.routes import stream_run_events

    db.init_db(config)

    async def collect():
        return [frame async for frame in stream_run_events(config, 999999, since_id=0)]

    frames = asyncio.run(collect())
    assert frames == []

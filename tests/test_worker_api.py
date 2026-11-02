"""Server side of the PC worker: token, claim, lease, attempts, release."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from autojob import db, set_worker_token, worker_token
from autojob.apply import apply as apply_module
from autojob.apply import mailer, remote
from autojob.pipeline import run as run_module

TOKEN = "t-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


@pytest.fixture
def api(config, monkeypatch):
    from autojob.web.app import create_app

    worker_token.set_worker_token(config, TOKEN)
    monkeypatch.setattr(
        remote.answers_module,
        "generate_application_content",
        lambda *a, **k: {"cover_letter": "Dear team", "screening_answers": {"why_us": "honest"}},
    )
    cv = config.cv_dir
    cv.mkdir(parents=True, exist_ok=True)
    (cv / "cv.pdf").write_bytes(b"%PDF-1.4 cv")
    app = create_app(config)
    return TestClient(app)


def _conn(config):
    return db.get_connection(config)


def _job(conn, url, *, site_url=None, cv_email=None, source="linkedin", name="Acme", title="Pentester"):
    company_id = db.get_or_create_company(conn, name)
    if cv_email:
        conn.execute("UPDATE companies SET cv_email = ? WHERE id = ?", (cv_email, company_id))
    job_id = db.create_job(conn, source=source, url=url, title=title, company_id=company_id, status="queued")
    if site_url:
        db.set_job_company_site_url(conn, job_id, site_url)
    conn.commit()
    return job_id


def _claim(api):
    return api.post("/api/worker/claim", json={"worker": "pc"}, headers=AUTH)


def _attempt(api, job_id, claimed, payload, files=()):
    body = {"index": 0, "kind": "company_job", "target": claimed["channels"][0]["target"],
            "session": "fill", "reason_code": "none", "final_url": "https://careers.example.com/apply"}
    body.update(payload)
    return api.post(
        f"/api/worker/jobs/{job_id}/attempts",
        data={"lease_token": claimed["lease_token"], "payload": json.dumps(body)},
        files=[("screenshots", (name, blob, "image/png")) for name, blob in files],
        headers=AUTH,
    )


def test_auth_required(api, config):
    assert api.post("/api/worker/claim").status_code == 401
    assert api.post("/api/worker/claim", headers={"Authorization": "Bearer nope"}).status_code == 401
    conn = _conn(config)
    assert db.get_setting(conn, "worker_last_seen") is None


def test_nothing_queued(api):
    assert _claim(api).status_code == 204


def test_claim_partition_and_lease(api, config):
    conn = _conn(config)
    gh = _job(conn, "https://boards.greenhouse.io/acme/jobs/1", name="GH", source="alljobs")
    job_id = _job(conn, "https://www.linkedin.com/jobs/view/9", site_url="https://careers.example.com/j/1",
                  cv_email="jobs@example.com")
    response = _claim(api)
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job_id != gh
    assert body["phase"] == "fill" and body["submit_allowed"] is False
    assert [c["kind"] for c in body["channels"]] == ["company_job", "job_url"]
    assert body["channels"][0]["index"] == 0
    assert not any(k.startswith("_") for k in body["answers"])
    assert body["cv"]["filename"] == "cv.pdf"
    job = db.get_job_with_company(conn, job_id)
    assert job["status"] == "applying" and job["worker_lease_token"] == body["lease_token"]
    application = db.get_application(conn, body["application_id"])
    assert application["apply_url"] == "https://careers.example.com/j/1"
    assert db.get_setting(conn, "worker_last_seen")
    assert _claim(api).status_code == 204
    assert db.get_job_with_company(conn, gh)["status"] == "queued"

    assert api.get("/api/worker/cv", headers=AUTH).content.startswith(b"%PDF")


def test_auto_submit_claim(api, config):
    conn = _conn(config)
    _job(conn, "https://careers.example.com/j/1")
    db.set_setting(conn, "auto_submit", True)
    assert _claim(api).json()["submit_allowed"] is True


def test_heartbeat(api, config):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    assert api.post(f"/api/worker/jobs/{job_id}/heartbeat", json={"lease_token": "bad"}, headers=AUTH).status_code == 409
    ok = api.post(f"/api/worker/jobs/{job_id}/heartbeat", json={"lease_token": claimed["lease_token"]}, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["lease_until"] >= claimed["lease_until"]


def test_captcha_falls_through_then_filled(api, config):
    conn = _conn(config)
    job_id = _job(conn, "https://www.linkedin.com/jobs/view/9", site_url="https://careers.example.com/j/1")
    claimed = _claim(api).json()
    r = _attempt(api, job_id, claimed, {"status": "needs_help", "reason_code": "captcha"},
                 files=[("filled.png", PNG)])
    assert r.json() == {"next_index": 1}
    application = db.get_application(conn, claimed["application_id"])
    assert "Tried company_job" in application["notes"]
    proofs = db.list_proofs(conn, claimed["application_id"])
    assert [p["kind"] for p in proofs] == ["screenshot"]
    assert f"applications/{claimed['application_id']}/" in proofs[0]["path"]

    r = _attempt(api, job_id, claimed, {
        "index": 1, "kind": "job_url", "target": claimed["channels"][1]["target"], "status": "filled",
        "fields_filled": [{"label": "Email", "value": "a@b.c"}], "cv_uploaded": True,
    })
    assert r.json() == {"done": True, "status": "ready_to_submit"}
    application = db.get_application(conn, claimed["application_id"])
    assert application["channel"] == "agent:careers.example.com"
    assert "Email=a@b.c" in application["notes"]
    job = db.get_job_with_company(conn, job_id)
    assert job["worker_lease_token"] is None and job["status"] == "applying"


def test_unanswered_required_needs_help(api, config):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    r = _attempt(api, job_id, claimed, {"status": "filled", "unanswered_required": ["Visa?"]})
    assert r.json()["status"] == "needs_help"
    assert len(db.list_open_help_items(conn)) == 1
    assert "no honest basis" in db.get_application(conn, claimed["application_id"])["notes"]


def test_no_form_on_last_page_runs_email(api, config, monkeypatch):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1", cv_email="jobs@example.com")
    monkeypatch.setattr(
        mailer, "draft_email",
        lambda **k: {"to": k["to"], "subject": "s", "body": "b", "attachment_name": "cv.pdf"},
    )
    claimed = _claim(api).json()
    assert [c["kind"] for c in claimed["channels"]] == ["job_url"]
    r = _attempt(api, job_id, claimed, {"status": "needs_help", "reason_code": "no_form"})
    assert r.json() == {"done": True, "status": "ready_to_submit"}
    application = db.get_application(conn, claimed["application_id"])
    assert application["channel"] == "email:example.com"


def test_submitted(api, config):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    r = _attempt(api, job_id, claimed, {"status": "submitted"}, files=[("submitted.png", PNG)])
    assert r.json() == {"done": True, "status": "sent"}
    assert db.get_job_with_company(conn, job_id)["status"] == "applied"
    assert "confirmation" in [p["kind"] for p in db.list_proofs(conn, claimed["application_id"])]


def test_bad_payload_and_big_upload(api, config):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    assert _attempt(api, job_id, claimed, {"status": "bogus"}).status_code == 400
    big = b"0" * (9 * 1024 * 1024)
    assert _attempt(api, job_id, claimed, {"status": "filled"}, files=[("a.png", big)]).status_code == 400


def test_lease_expiry(api, config, monkeypatch):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    assert db.requeue_interrupted_applying_jobs(conn) == 0
    monkeypatch.setattr(db, "now_iso", lambda: "2999-01-01T00:00:00Z")
    assert db.expire_worker_leases(conn, db.now_iso()) == 1
    assert db.get_job_with_company(conn, job_id)["status"] == "queued"
    application = db.get_application(conn, claimed["application_id"])
    assert application["status"] == "failed" and "lease expired" in application["notes"]


def test_release_deletes_or_fails_placeholder(api, config):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    r = api.post(f"/api/worker/jobs/{job_id}/release", json={"lease_token": claimed["lease_token"], "reason": "x"}, headers=AUTH)
    assert r.status_code == 200
    assert db.get_application(conn, claimed["application_id"]) is None
    assert db.get_job_with_company(conn, job_id)["status"] == "queued"

    claimed = _claim(api).json()
    db.create_proof(conn, application_id=claimed["application_id"], kind="screenshot", path="/x.png")
    api.post(f"/api/worker/jobs/{job_id}/release", json={"lease_token": claimed["lease_token"], "reason": "usage"}, headers=AUTH)
    application = db.get_application(conn, claimed["application_id"])
    assert application["status"] == "failed" and "usage" in application["notes"]


def test_release_application_queues_submit(api, config):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    _attempt(api, job_id, claimed, {"status": "filled"})
    result = apply_module.release_application(claimed["application_id"])
    assert result["status"] == "queued"
    again = _claim(api).json()
    assert again["phase"] == "submit" and again["submit_allowed"] is True
    assert len(again["channels"]) == 1 and again["application_id"] == claimed["application_id"]


def test_run_apply_phase_skips_worker_jobs(config, monkeypatch):
    conn = _conn(config)
    db.init_db(config)
    worker_job = _job(conn, "https://careers.example.com/j/1", name="W", source="alljobs")
    gh = _job(conn, "https://boards.greenhouse.io/acme/jobs/1", name="G", source="alljobs")
    seen = []
    monkeypatch.setattr(run_module, "apply_to_job", lambda job, **k: seen.append(job["id"]) or {"status": "ready_to_submit"})

    class Log:
        text = ""
        def log(self, m): pass

    run_module._run_apply_phase(conn, Log(), daily_cap=20, auto_submit=False)
    assert worker_job in seen  # no token: nothing changes
    seen.clear()
    db.update_job_status(conn, worker_job, "queued")
    db.update_job_status(conn, gh, "queued")
    worker_token.set_worker_token(config, TOKEN)
    run_module._run_apply_phase(conn, Log(), daily_cap=20, auto_submit=False)
    assert seen == [gh]


def test_set_worker_token_cli(config, capsys):
    assert set_worker_token.main([]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1]
    assert worker_token.verify_worker_token(config, token)
    assert not worker_token.verify_worker_token(config, "other")
    assert set_worker_token.main([]) == 1
    assert token not in capsys.readouterr().out
    assert set_worker_token.main(["--rotate"]) == 0


def test_runs_page_line(app, client, config, with_password):
    from tests.conftest import get_csrf_token

    token = get_csrf_token(client)
    client.post("/login", data={"password": with_password, "csrf_token": token})
    assert "PC worker" not in client.get("/runs").text
    worker_token.set_worker_token(config, TOKEN)
    assert "PC worker: never connected" in client.get("/runs").text


def test_old_applications_check_is_rebuilt(config):
    config.ensure_dirs()
    old_schema = db.SCHEMA.replace("            OR channel LIKE 'agent:%'\n", "")
    assert old_schema != db.SCHEMA
    raw = sqlite3.connect(config.db_path)
    raw.executescript(old_schema)
    raw.close()
    db.init_db(config)
    conn = _conn(config)
    job_id = _job(conn, "https://x.example.com/1")
    db.create_application(conn, job_id=job_id, channel="agent:example.com", status="ready_to_submit")


# --- pilot through the PC worker -------------------------------------------

def _no_server_apply(monkeypatch):
    def boom(job, **kwargs):
        raise AssertionError("a worker pilot must not apply on the server")

    monkeypatch.setattr(run_module, "apply_to_job", boom)


def test_pilot_with_token_binds_worker_claims(api, config, monkeypatch):
    _no_server_apply(monkeypatch)
    conn = _conn(config)
    gh = _job(conn, "https://boards.greenhouse.io/acme/jobs/1", name="GH")
    other = _job(conn, "https://careers.example.com/j/1", name="Ex")
    third = _job(conn, "https://careers.example.com/j/2", name="Ex2")
    db.set_setting(conn, "pilot_cap", 2)
    db.set_setting(conn, "auto_submit", True)

    run_id = run_module.run_scan(trigger="manual", mode="pilot")
    run = db.get_run(conn, run_id)
    assert run["mode"] == "pilot" and run["finished_at"] is None
    assert db.get_setting(conn, remote.PILOT_SETTING)["cap"] == 2

    first = _claim(api).json()
    second = _claim(api).json()
    assert _claim(api).status_code == 204
    claimed_jobs = {first["job_id"], second["job_id"]}
    assert claimed_jobs < {gh, other, third}
    assert gh in claimed_jobs  # the pilot hands the worker ATS jobs too
    for claimed in (first, second):
        assert claimed["submit_allowed"] is False and claimed["pilot_run_id"] == run_id
        assert db.get_application(conn, claimed["application_id"])["run_id"] == run_id

    _attempt(api, first["job_id"], first, {"status": "filled"}, files=[("filled.png", PNG)])
    assert db.get_run(conn, run_id)["finished_at"] is None  # second still in flight
    _attempt(api, second["job_id"], second, {"status": "needs_help", "reason_code": "captcha",
                                             "index": len(second["channels"]) - 1})
    run = db.get_run(conn, run_id)
    assert run["finished_at"] is not None and run["applied"] == 1
    assert "cap of 2" in run["log"]
    assert db.get_setting(conn, remote.PILOT_SETTING) is None
    rows = db.list_pilot_applications(conn, run_id)
    assert [r["id"] for r in rows] == [first["application_id"], second["application_id"]]
    assert [p["kind"] for p in db.list_proofs(conn, first["application_id"])] == ["screenshot"]


def test_pilot_window_closes_at_0700(api, config, monkeypatch):
    _no_server_apply(monkeypatch)
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    run_id = run_module.run_scan(trigger="manual", mode="pilot")
    until = db.get_setting(conn, remote.PILOT_SETTING)["until"]
    monkeypatch.setattr(db, "now_iso", lambda: until)
    claimed = _claim(api).json()
    assert claimed["job_id"] == job_id and claimed["pilot_run_id"] is None
    assert db.get_application(conn, claimed["application_id"])["run_id"] is None
    run = db.get_run(conn, run_id)
    assert run["finished_at"] is not None and "07:00" in run["log"]


def test_pilot_window_end_is_next_0700_local():
    from datetime import datetime, timezone

    # November: Asia/Jerusalem is UTC+2, so 07:00 local is 05:00Z.
    assert remote.pilot_window_end(datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)) == "2026-11-02T05:00:00Z"
    assert remote.pilot_window_end(datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)) == "2026-11-03T05:00:00Z"


def test_other_runs_leave_open_pilot_alone(api, config, monkeypatch):
    _no_server_apply(monkeypatch)
    conn = _conn(config)
    pilot_id = run_module.run_scan(trigger="manual", mode="pilot")
    run_module.run_scan(trigger="manual", mode="refresh", keywords=[])
    assert db.get_run(conn, pilot_id)["finished_at"] is None


def test_pilot_without_token_runs_on_server(config, monkeypatch):
    seen = []
    monkeypatch.setattr(run_module, "apply_to_job", lambda job, **k: seen.append(job["id"]) or {"status": "ready_to_submit"})
    db.init_db(config)
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    run_id = run_module.run_scan(trigger="manual", mode="pilot")
    assert seen == [job_id]
    assert db.get_run(conn, run_id)["finished_at"] is not None
    assert db.get_setting(conn, remote.PILOT_SETTING) is None


# --- contract: the worker's own client against the server routes -----------

def test_worker_client_matches_server_routes(api, config):
    from worker import api as worker_api
    from worker import worker as worker_script

    def transport(method, url, headers, body):
        path = url.split("://", 1)[-1].split("/", 1)[1]
        response = api.request(method, "/" + path, headers=headers, content=body)
        return response.status_code, response.content

    client = worker_api.Api("http://testserver", TOKEN, transport=transport)
    assert client.claim("pc") is None
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = client.claim("pc")
    assert claimed["job_id"] == job_id
    assert "lease_until" in client.heartbeat(job_id, claimed["lease_token"])

    class Session:
        last_url = ""
        screenshots = []
        denied = 0
        tool_calls = 3

    payload = worker_script.build_payload(
        {"status": "filled", "fields_filled": [{"label": "Email", "value": "a@b.c"}]},
        claimed["channels"][0], "fill", Session(),
    )
    assert payload["target"] == claimed["channels"][0]["target"]
    shot = config.data_dir / "filled.png"
    shot.write_bytes(PNG)
    answer = client.post_attempt(job_id, claimed["lease_token"], payload, [shot])
    assert answer == {"done": True, "status": "ready_to_submit"}
    proofs = db.list_proofs(conn, claimed["application_id"])
    assert [p["kind"] for p in proofs] == ["screenshot"] and proofs[0]["path"].endswith("filled.png")

    _job(conn, "https://careers.example.com/j/2", name="Two")
    again = client.claim("pc")
    client.release(again["job_id"], again["lease_token"], "test")
    assert db.get_job_with_company(conn, again["job_id"])["status"] == "queued"


# --- review fixes ------------------------------------------------------------

def _login(client, password):
    from tests.conftest import get_csrf_token

    client.post("/login", data={"password": password, "csrf_token": get_csrf_token(client)})
    return get_csrf_token(client)


def test_release_refused_while_submit_queued_or_leased(api, config):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    _attempt(api, job_id, claimed, {"status": "filled"})
    app_id = claimed["application_id"]
    assert apply_module.release_application(app_id)["status"] == "queued"
    # A second Release while the submit is queued changes nothing.
    assert apply_module.release_application(app_id)["status"] == "refused"
    submit = _claim(api).json()
    assert submit["phase"] == "submit"
    assert db.queue_worker_submit(conn, job_id, app_id) is False
    job = db.get_job_with_company(conn, job_id)
    assert job["worker_lease_token"] == submit["lease_token"] and job["status"] == "applying"
    row = next(a for a in db.list_applications_with_jobs(conn) if a["id"] == app_id)
    assert row["submit_pending"]
    assert db.get_application_with_job(conn, app_id)["submit_pending"]


def test_release_route_and_list_hide_release_while_submit_queued(api, config, with_password):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    _attempt(api, job_id, claimed, {"status": "filled"})
    app_id = claimed["application_id"]
    token = _login(api, with_password)
    page = api.get("/").text
    assert f"/applications/{app_id}/release" in page
    db.queue_worker_submit(conn, job_id, app_id)
    page = api.get("/").text
    assert f"/applications/{app_id}/release" not in page and "Submit queued" in page
    r = api.post(f"/applications/{app_id}/release", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 409


def test_apply_now_refused_while_worker_holds_job(api, config, with_password, monkeypatch):
    from autojob.web import routes

    started = []
    monkeypatch.setattr(routes, "_apply_job_in_background", lambda *a: started.append(a))
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    _claim(api)
    token = _login(api, with_password)
    r = api.post(f"/jobs/{job_id}/apply", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 409 and not started


def test_requeue_clears_expired_lease_and_fails_placeholder(api, config):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    conn.execute("UPDATE jobs SET worker_lease_until = '2000-01-01T00:00:00Z' WHERE id = ?", (job_id,))
    conn.commit()
    assert db.requeue_interrupted_applying_jobs(conn) == 1
    job = db.get_job_with_company(conn, job_id)
    assert job["status"] == "queued"
    assert job["worker_lease_token"] is None and job["worker_lease_until"] is None
    assert db.get_application(conn, claimed["application_id"])["status"] == "failed"


def test_screenshot_must_be_png_and_names_carry_phase(api, config):
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    r = _attempt(api, job_id, claimed, {"status": "filled"}, files=[("filled.png", b"<html>not a png")])
    assert r.status_code == 400 and "PNG" in r.text
    _attempt(api, job_id, claimed, {"status": "filled"}, files=[("filled.png", PNG)])
    apply_module.release_application(claimed["application_id"])
    submit = _claim(api).json()
    _attempt(api, job_id, submit, {"status": "submitted", "session": "fill_and_submit"},
             files=[("filled.png", PNG + b"submit"), ("submitted.png", PNG)])
    paths = [p["path"] for p in db.list_proofs(conn, claimed["application_id"]) if p["kind"] == "screenshot"]
    names = sorted(path.rsplit("/", 1)[-1] for path in paths)
    assert any("-fill-filled.png" in n for n in names)
    assert any("-submit-filled.png" in n for n in names)
    fill_shot = next(p for p in paths if p.endswith("-fill-filled.png"))
    assert open(fill_shot, "rb").read() == PNG


def test_claim_and_attempt_run_off_the_event_loop(api, config, monkeypatch):
    import asyncio

    def no_loop():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return True
        return False

    seen = {}
    real_claim, real_record = remote.claim_next, remote.record_attempt

    def claim_spy(*a, **k):
        seen["claim"] = no_loop()
        return real_claim(*a, **k)

    def record_spy(*a, **k):
        seen["record"] = no_loop()
        return real_record(*a, **k)

    monkeypatch.setattr(remote, "claim_next", claim_spy)
    monkeypatch.setattr(remote, "record_attempt", record_spy)
    conn = _conn(config)
    job_id = _job(conn, "https://careers.example.com/j/1")
    claimed = _claim(api).json()
    assert _attempt(api, job_id, claimed, {"status": "filled"}).status_code == 200
    assert seen == {"claim": True, "record": True}


def test_pilot_page_explains_worker_window(api, config, with_password):
    _login(api, with_password)
    assert "next 02:05 run" in api.get("/pilot").text

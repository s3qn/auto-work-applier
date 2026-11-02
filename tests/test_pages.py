"""Page rendering and form behavior tests, all against a logged-in session."""

from __future__ import annotations

import io
import re
import time

from tests.conftest import get_csrf_token, login


def test_all_pages_render_when_empty(client, with_password):
    login(client, with_password)
    for path in ["/", "/jobs", "/needs-help", "/submit", "/targets", "/runs", "/profile", "/browser", "/pilot"]:
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"


def test_jobs_page_lists_jobs_and_filters_by_status(client, with_password, config):
    login(client, with_password)
    from autojob import db

    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        db.create_job(
            conn, source="linkedin", url="https://example.com/a", title="Pentester",
            company_id=company_id, status="queued", fit_score=8.0, reason="good fit",
        )
        db.create_job(
            conn, source="linkedin", url="https://example.com/b", title="Sales Rep",
            company_id=company_id, status="filtered_out", fit_score=1.0, reason="not security",
        )
    finally:
        conn.close()

    response = client.get("/jobs")
    assert response.status_code == 200
    assert "Pentester" in response.text
    assert "Sales Rep" in response.text

    queued_only = client.get("/jobs?status=queued")
    assert "Pentester" in queued_only.text
    assert "Sales Rep" not in queued_only.text

    filtered_only = client.get("/jobs?status=filtered_out")
    assert "Sales Rep" in filtered_only.text
    assert "Pentester" not in filtered_only.text


def test_jobs_queued_tab_shows_direct_channel_and_research_button(client, with_password, config):
    login(client, with_password)
    from autojob import db

    conn = db.get_connection(config)
    try:
        site_company = db.get_or_create_company(conn, "SiteCo")
        job_id = db.create_job(
            conn, source="linkedin", url="https://example.com/site", title="Pentester",
            company_id=site_company, status="queued",
        )
        db.set_job_company_site_url(conn, job_id, "https://siteco.example/careers/123456")
        email_company = db.get_or_create_company(conn, "EmailCo")
        db.update_company_research(
            conn, email_company, website=None, tldr="Does things.", careers_url=None,
            cv_email="jobs@emailco.example", cv_email_source_url="https://emailco.example/contact",
        )
        db.create_job(
            conn, source="linkedin", url="https://example.com/email", title="Red Teamer",
            company_id=email_company, status="queued",
        )
    finally:
        conn.close()

    response = client.get("/jobs?status=queued")
    assert response.status_code == 200
    assert "site" in response.text
    assert "email" in response.text
    assert "Research companies for direct apply channels" in response.text


def test_jobs_research_pending_runs_in_background(client, with_password, config, monkeypatch):
    login(client, with_password)
    from autojob.pipeline import run as run_module
    from autojob.web import routes as routes_module

    calls = []

    def fake_research_pending(conn, *, work_dir, limit):
        calls.append(limit)
        return 0

    monkeypatch.setattr(routes_module, "research_pending", fake_research_pending)

    token = get_csrf_token(client, "/jobs")
    response = client.post("/jobs/research-pending", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs?status=queued&research=started"

    for _ in range(40):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [run_module.MAX_RESEARCH_PER_RUN]


def test_company_tldr_partial_renders(client, with_password, config):
    login(client, with_password)
    from autojob import db

    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        db.update_company_research(
            conn, company_id, website="https://acme.example", tldr="Acme does security consulting.",
            careers_url="https://acme.example/careers",
        )
    finally:
        conn.close()

    response = client.get(f"/companies/{company_id}/tldr")
    assert response.status_code == 200
    assert "Acme does security consulting." in response.text
    assert "https://acme.example" in response.text


def test_found_today_stat_counts_jobs_found_today(client, with_password, config):
    login(client, with_password)
    from autojob import db

    conn = db.get_connection(config)
    try:
        db.create_job(conn, source="manual", url="https://example.com/x", title="Pentester")
    finally:
        conn.close()

    response = client.get("/")
    assert response.status_code == 200
    stats_html = response.text
    assert "Found today" in stats_html


def test_applications_empty_state(client, with_password):
    login(client, with_password)
    response = client.get("/")
    assert "No applications yet" in response.text


def test_submit_link_creates_queued_manual_job(client, with_password, config):
    login(client, with_password)
    token = get_csrf_token(client, "/submit")
    response = client.post(
        "/submit",
        data={"url": "https://example.com/job/123", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "example.com/job/123" in response.text

    from autojob import db

    conn = db.get_connection(config)
    try:
        jobs = db.list_jobs(conn, source="manual")
    finally:
        conn.close()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["url"] == "https://example.com/job/123"


def test_submit_link_rejects_non_http_url(client, with_password):
    login(client, with_password)
    token = get_csrf_token(client, "/submit")
    response = client.post(
        "/submit", data={"url": "javascript:alert(1)", "csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 200
    assert "valid http" in response.text


def test_targets_save_and_reload(client, with_password):
    login(client, with_password)
    token = get_csrf_token(client, "/targets")
    response = client.post(
        "/targets",
        data={
            "titles": "Pentester\nRed Team",
            "locations": "Israel\nRemote",
            "max_years": "5",
            "excluded_companies": "BadCo",
            "priority_companies": "GoodCo",
            "daily_cap_per_source": "10",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Saved." in response.text

    reload_response = client.get("/targets")
    assert "Pentester" in reload_response.text
    assert "Red Team" in reload_response.text
    assert "BadCo" in reload_response.text
    assert "08:00 Asia/Jerusalem" in reload_response.text


def test_cv_upload_rejects_other_file_types(client, with_password):
    login(client, with_password)
    token = get_csrf_token(client, "/profile")
    response = client.post(
        "/profile/cv",
        data={"csrf_token": token},
        files={"cv_file": ("resume.txt", io.BytesIO(b"not a real cv"), "text/plain")},
    )
    assert response.status_code == 200
    assert "Only PDF and DOCX" in response.text


def test_cv_upload_accepts_pdf(client, with_password):
    login(client, with_password)
    token = get_csrf_token(client, "/profile")
    response = client.post(
        "/profile/cv",
        data={"csrf_token": token},
        files={"cv_file": ("resume.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
    )
    assert response.status_code == 200
    assert "resume.pdf" in response.text


def test_run_now_records_a_run(client, with_password, config, monkeypatch):
    # /runs/now runs the real pipeline in a background thread. Stub out the
    # sources so this test stays fast and touches no network or claude CLI.
    from autojob.sources import alljobs, jobmaster, linkedin

    monkeypatch.setattr(linkedin, "search", lambda keyword, **kw: [])
    monkeypatch.setattr(alljobs, "search", lambda keyword: [])
    monkeypatch.setattr(jobmaster, "search", lambda keyword: [])

    login(client, with_password)
    token = get_csrf_token(client, "/runs")
    response = client.post("/runs/now", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303

    from autojob import db

    conn = db.get_connection(config)
    try:
        runs = []
        for _ in range(40):
            runs = db.list_runs(conn)
            if runs and runs[0]["finished_at"]:
                break
            time.sleep(0.05)
    finally:
        conn.close()

    assert runs, "expected at least one run to be recorded"
    assert runs[0]["trigger"] == "manual"
    assert runs[0]["finished_at"] is not None


def test_refresh_records_a_refresh_run(client, with_password, config, monkeypatch):
    from autojob import db
    from autojob.pipeline import run as run_module
    from autojob.sources import alljobs, jobmaster, linkedin

    monkeypatch.setattr(linkedin, "search", lambda keyword, **kw: [])
    monkeypatch.setattr(alljobs, "search", lambda keyword: [])
    monkeypatch.setattr(jobmaster, "search", lambda keyword: [])

    def fail(*a, **kw):
        raise AssertionError("research and apply must not run on a refresh")

    monkeypatch.setattr(run_module, "research_pending", fail)
    monkeypatch.setattr(run_module, "apply_to_job", fail)

    login(client, with_password)
    token = get_csrf_token(client, "/jobs")
    response = client.post("/runs/refresh", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs?refresh=started"

    conn = db.get_connection(config)
    try:
        runs = []
        for _ in range(40):
            runs = db.list_runs(conn)
            if runs and runs[0]["finished_at"]:
                break
            time.sleep(0.05)
    finally:
        conn.close()
    assert runs and runs[0]["mode"] == "refresh"


def test_refresh_redirects_busy_when_lock_held(client, with_password, config):
    import fcntl

    from autojob import db

    login(client, with_password)
    token = get_csrf_token(client, "/jobs")
    handle = open(config.run_lock_path, "a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        response = client.post("/runs/refresh", data={"csrf_token": token}, follow_redirects=False)
    finally:
        handle.close()
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs?busy=1"
    conn = db.get_connection(config)
    try:
        assert db.list_runs(conn) == []
    finally:
        conn.close()


def test_jobs_page_shows_refresh_button_and_flash(client, with_password):
    login(client, with_password)
    response = client.get("/jobs?refresh=started")
    assert "Refresh jobs" in response.text
    assert "Watch it live" in response.text


def test_sidebar_shows_computed_next_scan(client, with_password):
    login(client, with_password)
    response = client.get("/jobs")
    assert re.search(r"Next scan (today|tomorrow) at 08:00", response.text)


# --- job detail and apply now --------------------------------------------

def test_job_detail_page_renders(client, with_password, config):
    login(client, with_password)
    from autojob import db

    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        job_id = db.create_job(
            conn, source="linkedin", url="https://example.com/job", title="Junior Pentester",
            company_id=company_id, status="queued",
        )
    finally:
        conn.close()

    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    assert "Junior Pentester" in response.text
    assert "Apply now" in response.text


def test_job_apply_now_runs_apply_to_job_in_background(client, with_password, config, monkeypatch):
    login(client, with_password)
    from autojob import db

    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        job_id = db.create_job(
            conn, source="linkedin", url="https://example.com/job", title="Junior Pentester",
            company_id=company_id, status="queued",
        )
    finally:
        conn.close()

    calls = []

    def fake_apply_to_job(job, *, auto_submit):
        calls.append((job["id"], auto_submit))
        return {"status": "ready_to_submit", "application_id": None, "reason": None}

    monkeypatch.setattr("autojob.apply.apply.apply_to_job", fake_apply_to_job)

    token = get_csrf_token(client, f"/jobs/{job_id}")
    response = client.post(f"/jobs/{job_id}/apply", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303

    for _ in range(40):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [(job_id, False)]  # auto_submit defaults to off


def test_job_apply_now_is_busy_while_a_run_holds_the_lock(client, with_password, config, monkeypatch):
    login(client, with_password)
    from autojob import db
    from autojob.pipeline import lock as runlock

    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        job_id = db.create_job(
            conn, source="linkedin", url="https://example.com/job2", title="Junior Pentester",
            company_id=company_id, status="queued",
        )
    finally:
        conn.close()

    calls = []
    monkeypatch.setattr("autojob.apply.apply.apply_to_job", lambda job, *, auto_submit: calls.append(job["id"]))

    token = get_csrf_token(client, f"/jobs/{job_id}")
    with runlock.hold_run_lock(config):
        response = client.post(f"/jobs/{job_id}/apply", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job_id}?busy=1"
    time.sleep(0.2)
    assert calls == []


# --- targets: auto_submit toggle ------------------------------------------

def test_targets_page_shows_auto_submit_explanation_and_defaults_off(client, with_password):
    login(client, with_password)
    response = client.get("/targets")
    assert response.status_code == 200
    assert "auto_submit" in response.text
    assert "submitted automatically" in response.text
    assert 'name="auto_submit" value="true" ' in response.text  # unchecked: no "checked" right after


def test_targets_save_enables_auto_submit(client, with_password, config):
    login(client, with_password)
    token = get_csrf_token(client, "/targets")
    response = client.post(
        "/targets",
        data={
            "titles": "Pentester", "locations": "Israel", "max_years": "3",
            "excluded_companies": "", "priority_companies": "", "daily_cap_per_source": "10",
            "auto_submit": "true", "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200

    from autojob import db
    from autojob.web.deps import get_auto_submit

    conn = db.get_connection(config)
    try:
        assert get_auto_submit(conn) is True
    finally:
        conn.close()

    reload_response = client.get("/targets")
    assert 'name="auto_submit" value="true" checked' in reload_response.text


# --- application release / discard / proof files ---------------------------

def _seed_ready_to_submit_application(config):
    from autojob import db

    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        job_id = db.create_job(
            conn, source="manual", url="https://example.com/job", title="Pentester",
            company_id=company_id, status="applying",
        )
        application_id = db.create_application(conn, job_id=job_id, channel="greenhouse", status="ready_to_submit")
    finally:
        conn.close()
    return job_id, application_id


def test_application_discard_marks_discarded_and_fails_the_job(client, with_password, config):
    login(client, with_password)
    job_id, application_id = _seed_ready_to_submit_application(config)

    token = get_csrf_token(client, f"/applications/{application_id}")
    response = client.post(
        f"/applications/{application_id}/discard", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303

    from autojob import db

    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, application_id)
        job = db.get_job(conn, job_id)
    finally:
        conn.close()
    assert application["status"] == "discarded"
    assert job["status"] == "failed"


def test_application_discard_rejects_non_ready_application(client, with_password, config):
    login(client, with_password)
    from autojob import db

    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        job_id = db.create_job(
            conn, source="manual", url="https://example.com/job2", title="Pentester",
            company_id=company_id, status="applied",
        )
        application_id = db.create_application(conn, job_id=job_id, channel="greenhouse", status="sent")
    finally:
        conn.close()

    token = get_csrf_token(client, f"/applications/{application_id}")
    response = client.post(
        f"/applications/{application_id}/discard", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 400


def test_application_release_runs_release_application_in_background(client, with_password, config, monkeypatch):
    login(client, with_password)
    job_id, application_id = _seed_ready_to_submit_application(config)

    calls = []
    monkeypatch.setattr(
        "autojob.apply.apply.release_application",
        lambda app_id: calls.append(app_id) or {"status": "sent", "reason": None},
    )

    token = get_csrf_token(client, f"/applications/{application_id}")
    response = client.post(
        f"/applications/{application_id}/release", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303

    for _ in range(40):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [application_id]


def test_proof_file_is_served_only_to_logged_in_user(client, with_password, config):
    from autojob import db

    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        job_id = db.create_job(
            conn, source="manual", url="https://example.com/job3", title="Pentester", company_id=company_id
        )
        application_id = db.create_application(conn, job_id=job_id, channel="greenhouse", status="sent")
        proof_path = config.data_dir / "applications" / str(application_id)
        proof_path.mkdir(parents=True, exist_ok=True)
        file_path = proof_path / "form.png"
        file_path.write_bytes(b"fake png bytes")
        proof_id = db.create_proof(conn, application_id=application_id, kind="screenshot", path=str(file_path))
    finally:
        conn.close()

    # Not logged in: redirected to login, never served the file.
    anon_response = client.get(f"/applications/{application_id}/proofs/{proof_id}", follow_redirects=False)
    assert anon_response.status_code == 303
    assert anon_response.headers["location"] == "/login"

    login(client, with_password)
    response = client.get(f"/applications/{application_id}/proofs/{proof_id}")
    assert response.status_code == 200
    assert response.content == b"fake png bytes"
    assert response.headers["content-type"] == "image/png"


# --- email channel: detail page card and targets settings -----------------

def test_application_detail_page_renders_email_card(client, with_password, config):
    login(client, with_password)
    import json

    from autojob import db

    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        job_id = db.create_job(
            conn, source="manual", url="https://example.com/job4", title="Pentester",
            company_id=company_id, status="applying",
        )
        draft = {
            "to": "jobs@acme.example.com",
            "subject": "Application for Pentester (Sean)",
            "body": "Dear hiring team,\n\nEmail: sean@example.com",
            "attachment_name": "cv.pdf",
        }
        application_id = db.create_application(
            conn, job_id=job_id, channel="email:acme.example.com", status="ready_to_submit",
            email_to=draft["to"], email_draft=json.dumps(draft),
        )
    finally:
        conn.close()

    response = client.get(f"/applications/{application_id}")
    assert response.status_code == 200
    assert "jobs@acme.example.com" in response.text
    assert "Application for Pentester (Sean)" in response.text
    assert "cv.pdf" in response.text
    assert "Release (send email)" in response.text


def test_targets_save_email_daily_cap(client, with_password, config):
    login(client, with_password)
    token = get_csrf_token(client, "/targets")
    response = client.post(
        "/targets",
        data={
            "titles": "Pentester", "locations": "Israel", "max_years": "3",
            "excluded_companies": "", "priority_companies": "", "daily_cap_per_source": "10",
            "email_daily_cap": "3", "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200

    from autojob import db
    from autojob.web.deps import get_email_daily_cap

    conn = db.get_connection(config)
    try:
        assert get_email_daily_cap(conn) == 3
    finally:
        conn.close()

    reload_response = client.get("/targets")
    assert 'id="email_daily_cap" name="email_daily_cap" min="0" value="3"' in reload_response.text
    assert "SMTP: not configured" in reload_response.text


# --- pilot page ------------------------------------------------------------

def _seed_pilot(config):
    from autojob import db

    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual", mode="pilot")
        ids = []
        for n, channel in enumerate(["comeet", "generic:x.com", "email:y.com"], start=1):
            company_id = db.get_or_create_company(conn, f"Co{n}")
            job_id = db.create_job(
                conn, source="linkedin", url=f"https://e.com/{n}", title=f"Job {n}", company_id=company_id,
                status="queued", dedup_key=f"k{n}",
            )
            ids.append(db.create_application(
                conn, job_id=job_id, channel=channel, status="ready_to_submit",
                answers_json='{"Why us?": "Because"}', run_id=run_id,
            ))
        shot = config.data_dir / "shot.png"
        shot.write_bytes(b"png")
        proof_id = db.create_proof(conn, application_id=ids[0], kind="screenshot", path=str(shot))
        return run_id, ids, proof_id
    finally:
        conn.close()


def test_pilot_empty_state(client, with_password):
    login(client, with_password)
    response = client.get("/pilot")
    assert response.status_code == 200
    assert "No pilot has run yet. It tries up to 20 queued jobs without submitting anything." in response.text
    assert "Run pilot now" in response.text
    assert "Disabled: no queued jobs." in response.text


def test_pilot_requires_login(client, with_password):
    response = client.get("/pilot", follow_redirects=False)
    assert response.status_code in (302, 303, 307)
    assert "/login" in response.headers["location"]


def test_pilot_lists_rows_and_proof_links(client, with_password, config):
    run_id, ids, proof_id = _seed_pilot(config)
    login(client, with_password)
    text = client.get("/pilot").text
    for title in ("Job 1", "Job 2", "Job 3"):
        assert title in text
    assert f"/applications/{ids[0]}/proofs/{proof_id}" in text
    assert "1 answer filled" in text


def test_pilot_verdict_set_clear_and_validation(client, with_password, config):
    from autojob import db

    run_id, ids, _ = _seed_pilot(config)
    login(client, with_password)
    token = get_csrf_token(client, "/pilot")
    url = f"/pilot/{ids[0]}/verdict"
    assert client.post(url, data={"csrf_token": token, "verdict": "right"}, follow_redirects=False).status_code == 303

    def verdict():
        conn = db.get_connection(config)
        try:
            return db.get_application(conn, ids[0])["pilot_verdict"]
        finally:
            conn.close()

    assert verdict() == "right"
    htmx = client.post(url, data={"csrf_token": token, "verdict": "wrong"}, headers={"HX-Request": "true"})
    assert htmx.status_code == 200 and "pilot-row-" in htmx.text
    assert verdict() == "wrong"
    client.post(url, data={"csrf_token": token, "verdict": "clear"}, follow_redirects=False)
    assert verdict() is None
    assert client.post(url, data={"csrf_token": token, "verdict": "maybe"}).status_code == 400
    assert client.post(url, data={"csrf_token": "bad", "verdict": "right"}).status_code == 403


def test_pilot_summary_math(client, with_password, config):
    from autojob import db

    run_id, ids, _ = _seed_pilot(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Extra")
        for n in (10, 11):
            job_id = db.create_job(
                conn, source="linkedin", url=f"https://e.com/{n}", title=f"J{n}", company_id=company_id,
                status="queued", dedup_key=f"k{n}",
            )
            ids.append(db.create_application(conn, job_id=job_id, channel="comeet", run_id=run_id))
        db.set_pilot_verdict(conn, ids[0], "right")
        db.set_pilot_verdict(conn, ids[3], "right")
        db.set_pilot_verdict(conn, ids[4], "wrong")
        summary = {g["channel"]: g for g in db.pilot_summary(conn, run_id)}
    finally:
        conn.close()
    assert summary["comeet"]["percent"] == 67
    assert summary["comeet"]["total"] == 3
    assert summary["generic"]["unreviewed"] == 1 and summary["generic"]["percent"] is None
    assert "email" in summary


def test_pilot_run_busy_when_lock_held(client, with_password, config):
    import fcntl

    from autojob import db

    login(client, with_password)
    token = get_csrf_token(client, "/pilot")
    handle = open(config.run_lock_path, "a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        response = client.post("/pilot/run", data={"csrf_token": token}, follow_redirects=False)
    finally:
        handle.close()
    assert response.headers["location"] == "/pilot?busy=1"
    conn = db.get_connection(config)
    try:
        assert db.list_runs(conn) == []
    finally:
        conn.close()


def test_pilot_toggle_and_cap_persist(client, with_password, config):
    login(client, with_password)
    token = get_csrf_token(client, "/pilot")
    client.post("/pilot/tonight", data={"csrf_token": token, "enabled": "on"}, follow_redirects=False)
    client.post("/pilot/cap", data={"csrf_token": token, "cap": "7"}, follow_redirects=False)
    text = client.get("/pilot").text
    assert 'value="7"' in text
    assert re.search(r'name="enabled"[^>]*checked', text)
    assert client.post("/pilot/cap", data={"csrf_token": token, "cap": "101"}).status_code == 400
    client.post("/pilot/tonight", data={"csrf_token": token}, follow_redirects=False)
    assert not re.search(r'name="enabled"[^>]*checked', client.get("/pilot").text)


def test_pilot_application_release_needs_confirmation(client, with_password, config, monkeypatch):
    login(client, with_password)
    run_id, ids, _ = _seed_pilot(config)
    calls = []
    monkeypatch.setattr(
        "autojob.apply.apply.release_application",
        lambda app_id: calls.append(app_id) or {"status": "sent", "reason": None},
    )
    page = client.get(f"/applications/{ids[0]}")
    assert "I checked the screenshots for this pilot application" in page.text
    token = get_csrf_token(client, f"/applications/{ids[0]}")
    response = client.post(
        f"/applications/{ids[0]}/release", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 400
    response = client.post(
        f"/applications/{ids[0]}/release",
        data={"csrf_token": token, "pilot_checked": "on"}, follow_redirects=False,
    )
    assert response.status_code == 303
    for _ in range(40):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [ids[0]]


def test_non_pilot_detail_has_no_pilot_checkbox(client, with_password, config):
    login(client, with_password)
    _, application_id = _seed_ready_to_submit_application(config)
    assert "pilot application" not in client.get(f"/applications/{application_id}").text


def test_pilot_page_rejects_non_pilot_run(client, with_password, config):
    from autojob import db

    login(client, with_password)
    _seed_pilot(config)
    conn = db.get_connection(config)
    try:
        full_id = db.create_run(conn, trigger="manual", mode="full")
    finally:
        conn.close()
    assert client.get(f"/pilot?run={full_id}").status_code == 404

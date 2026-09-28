"""Page rendering and form behavior tests, all against a logged-in session."""

from __future__ import annotations

import io
import time

from tests.conftest import get_csrf_token, login


def test_all_pages_render_when_empty(client, with_password):
    login(client, with_password)
    for path in ["/", "/jobs", "/needs-help", "/submit", "/targets", "/runs", "/profile"]:
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
            "scan_times": "08:00\n17:30",
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
    assert "08:00" in reload_response.text


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
            "scan_times": "", "auto_submit": "true", "csrf_token": token,
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
            "scan_times": "", "email_daily_cap": "3", "csrf_token": token,
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

"""Page rendering and form behavior tests, all against a logged-in session."""

from __future__ import annotations

import io
import time

from tests.conftest import get_csrf_token, login


def test_all_pages_render_when_empty(client, with_password):
    login(client, with_password)
    for path in ["/", "/needs-help", "/submit", "/targets", "/runs", "/profile"]:
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"


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


def test_run_now_records_a_run(client, with_password, config):
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

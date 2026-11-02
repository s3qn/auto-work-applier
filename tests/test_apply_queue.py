"""Daily apply queue: token, batch, selection, results, migration, pages."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from autojob import apply_token, db, worker_token
from autojob.apply import answers as answers_module
from autojob.apply import queue, remote
from autojob.pipeline import run as run_module

TOKEN = "apply-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LETTER = "Dear team,\n\nI am a junior pentester. I hold CPTS.\n\nThank you.\n\nSean"


@pytest.fixture
def env(config, monkeypatch):
    from autojob.web.app import create_app

    calls = []

    def fake(*a, **k):
        calls.append(a[2]["title"])
        return {"cover_letter": LETTER, "screening_answers": {}}

    monkeypatch.setattr(answers_module, "generate_application_content", fake)
    apply_token.set_apply_token(config, TOKEN)
    config.cv_dir.mkdir(parents=True, exist_ok=True)
    (config.cv_dir / "cv.pdf").write_bytes(b"%PDF-1.4 cv")
    app = create_app(config)
    return TestClient(app), config, calls


def _job(config, n, *, status="queued", fit=5.0, url=None, reason=None, source="linkedin"):
    conn = db.get_connection(config)
    cid = db.get_or_create_company(conn, f"Co{n}")
    jid = db.create_job(conn, source=source, url=url or f"https://www.linkedin.com/jobs/view/{n}",
                        title=f"Role{n}", company_id=cid, status=status)
    conn.execute("UPDATE jobs SET fit_score = ?, reason = ? WHERE id = ?", (fit, reason, jid))
    conn.commit()
    conn.close()
    return jid


def _ids(resp):
    return [j["id"] for j in resp.json()["jobs"]]


def test_auth(env):
    client, config, _ = env
    worker_token.set_worker_token(config, "w")
    assert client.get("/api/apply-queue").status_code == 401
    assert client.get("/api/apply-queue", headers={"Authorization": "Bearer w"}).status_code == 401
    assert client.post("/api/apply-result", json={}, headers={"Authorization": "Bearer w"}).status_code == 401
    assert client.get("/api/cv/latest").status_code == 401
    assert worker_token.worker_configured(config)


def test_same_batch_twice_and_trim(env):
    client, config, calls = env
    a, b = _job(config, 1), _job(config, 2)
    first = client.get("/api/apply-queue", headers=AUTH).json()
    second = client.get("/api/apply-queue", headers=AUTH).json()
    assert first == second and first["batch_id"]
    assert sorted(calls) == ["Role1", "Role2"]
    job = first["jobs"][0]
    assert "junior" not in job["cover_letter"].lower()
    assert job["answers"]["website"] == "https://sean.build"
    assert job["site"] == "linkedin" and first["cv_version"].startswith("cv.pdf@")
    assert {j["id"] for j in first["jobs"]} == {a, b}


def test_order_size_and_exclusions(env):
    client, config, _ = env
    low = _job(config, 1, fit=1)
    high = _job(config, 2, fit=9)
    _job(config, 3, status="applied")
    _job(config, 4, status="failed")
    _job(config, 5, status="filtered_out")
    _job(config, 6, reason="vip: x")
    _job(config, 7, url="https://www.alljobs.co.il/User/Register/CreditCard.aspx?x=1", source="alljobs")
    leased = _job(config, 8)
    conn = db.get_connection(config)
    conn.execute("UPDATE jobs SET worker_lease_token='t', worker_lease_until='2999-01-01T00:00:00Z' WHERE id=?", (leased,))
    conn.commit()
    sent = _job(config, 9)
    db.create_application(conn, job_id=sent, channel="other", status="sent")
    db.set_setting(conn, "apply_queue_size", 1)
    conn.commit()
    resp = client.get("/api/apply-queue", headers=AUTH)
    assert _ids(resp) == [high]
    assert resp.json()["size"] == 1
    assert low not in db.open_batch_job_ids(conn)


def test_bad_size_falls_back(env):
    _, config, _ = env
    conn = db.get_connection(config)
    db.set_setting(conn, "apply_queue_size", "abc")
    assert queue.queue_size(conn) == 20
    db.set_setting(conn, "apply_queue_size", 500)
    assert queue.queue_size(conn) == 50


def test_empty_and_new_day_expires(env):
    client, config, _ = env
    assert client.get("/api/apply-queue", headers=AUTH).json()["jobs"] == []
    jid = _job(config, 1)
    batch = client.get("/api/apply-queue", headers=AUTH).json()
    assert _ids(client.get("/api/apply-queue", headers=AUTH)) == [jid]
    conn = db.get_connection(config)
    conn.execute("UPDATE apply_batches SET day = '2000-01-01'")
    conn.commit()
    again = client.get("/api/apply-queue", headers=AUTH).json()
    assert again["batch_id"] != batch["batch_id"] and _ids_of(again) == [jid]
    assert conn.execute("SELECT state FROM apply_batch_items WHERE batch_id = ?", (batch["batch_id"],)).fetchone()[0] == "expired"


def _ids_of(body):
    return [j["id"] for j in body["jobs"]]


def _post(client, **body):
    return client.post("/api/apply-result", json=body, headers=AUTH)


def test_results(env):
    client, config, _ = env
    ok, vip, retry = _job(config, 1), _job(config, 2), _job(config, 3)
    client.get("/api/apply-queue", headers=AUTH)
    conn = db.get_connection(config)

    assert _post(client, id=ok, status="submitted", cover_letter_used="CL", cv_version="cv.pdf@1").json() == {"ok": True}
    assert _post(client, id=ok, status="submitted").json()["duplicate"] is True
    app = conn.execute("SELECT * FROM applications WHERE job_id = ?", (ok,)).fetchone()
    assert (app["status"], app["channel"], app["cv_version"], app["cover_letter"]) == ("sent", "pc:linkedin", "cv.pdf@1", "CL")
    assert db.get_job(conn, ok)["status"] == "applied"

    _post(client, id=vip, status="blocked", reason="VIP only")
    job = db.get_job(conn, vip)
    assert job["status"] == "failed" and job["reason"].startswith("vip:")

    for _ in range(3):
        _post(client, id=retry, status="failed", reason="timeout")
        conn.execute("UPDATE apply_batch_items SET state = 'open', reason = NULL WHERE job_id = ?", (retry,))
        conn.commit()
        _post(client, id=retry, status="failed", reason="timeout2")
        conn.execute("UPDATE apply_batch_items SET state = 'open', reason = NULL WHERE job_id = ?", (retry,))
        conn.commit()
    job = db.get_job(conn, retry)
    assert job["pc_attempts"] == 6 and job["status"] == "failed"

    assert _post(client, id=9999, status="failed").status_code == 404
    assert _post(client, id=ok, status="weird").status_code == 422


def test_failed_repeat_does_not_double_count(env):
    client, config, _ = env
    jid = _job(config, 1)
    client.get("/api/apply-queue", headers=AUTH)
    _post(client, id=jid, status="failed", reason="x")
    assert _post(client, id=jid, status="failed", reason="x").json()["duplicate"] is True
    conn = db.get_connection(config)
    assert db.get_job(conn, jid)["pc_attempts"] == 1
    assert db.get_job(conn, jid)["status"] == "queued"
    # submitted overwrites an earlier failure
    _post(client, id=jid, status="submitted")
    assert db.get_job(conn, jid)["status"] == "applied"


def test_result_without_batch_creates_application(env):
    client, config, _ = env
    jid = _job(config, 1)
    assert _post(client, id=jid, status="submitted", timestamp="2026-01-02T03:04:05+00:00").status_code == 200
    conn = db.get_connection(config)
    app = conn.execute("SELECT * FROM applications WHERE job_id = ?", (jid,)).fetchone()
    assert app["channel"] == "pc:linkedin" and app["applied_at"] == "2026-01-02T03:04:05Z"


def test_open_batch_jobs_skipped_by_claim_and_apply_phase(env, monkeypatch):
    client, config, _ = env
    jid = _job(config, 1)
    client.get("/api/apply-queue", headers=AUTH)
    conn = db.get_connection(config)
    assert remote.claim_next(conn, config, "pc") is None
    seen = []
    monkeypatch.setattr(run_module, "apply_to_job", lambda *a, **k: seen.append(a) or None, raising=False)
    logger = run_module.RunLogger(conn, db.create_run(conn, trigger="manual"))
    assert run_module._run_apply_phase(conn, logger, daily_cap=5, auto_submit=False) == (0, 0)
    assert seen == [] and db.get_job(conn, jid)["status"] == "queued"


def test_cv_latest(env):
    client, config, _ = env
    resp = client.get("/api/cv/latest", headers=AUTH)
    assert resp.status_code == 200 and resp.headers["X-CV-Version"].startswith("cv.pdf@")
    (config.cv_dir / "cv.pdf").unlink()
    assert client.get("/api/cv/latest", headers=AUTH).status_code == 404


def test_prebuild_never_fails_run(env, monkeypatch):
    _, config, _ = env
    _job(config, 1)
    conn = db.get_connection(config)
    run_id = db.create_run(conn, trigger="scheduled")
    logger = run_module.RunLogger(conn, run_id)
    run_module._prebuild_apply_queue(conn, config, logger, run_id)
    assert db.get_batch_for_day(conn, db.jerusalem_day()) is not None
    monkeypatch.setattr(queue, "prebuild_today", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    run_module._prebuild_apply_queue(conn, config, logger, run_id)
    assert "pre-build failed" in logger.text


def test_old_schema_migrates_and_keeps_rows(config):
    config.ensure_dirs()
    raw = sqlite3.connect(config.db_path)
    raw.executescript("""
    CREATE TABLE companies (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, website TEXT, tldr TEXT, researched_at TEXT);
    CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
        company_id INTEGER, location TEXT, description TEXT, found_at TEXT NOT NULL, fit_score REAL,
        status TEXT NOT NULL DEFAULT 'new', dedup_key TEXT NOT NULL UNIQUE);
    CREATE TABLE applications (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, channel TEXT NOT NULL
        CHECK (channel IN ('comeet','greenhouse','lever','email','google_form','linkedin','other')
            OR channel LIKE 'generic:%' OR channel LIKE 'email:%' OR channel LIKE 'agent:%'),
        applied_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent', notes TEXT, cover_letter TEXT, answers_json TEXT,
        cv_path TEXT, apply_url TEXT, email_to TEXT, email_draft TEXT, run_id INTEGER, pilot_verdict TEXT);
    INSERT INTO jobs (source,url,title,found_at,dedup_key) VALUES ('linkedin','u','t','2026-01-01T00:00:00Z','k');
    INSERT INTO applications (job_id,channel,applied_at,status,run_id,pilot_verdict,cover_letter)
        VALUES (1,'agent:x','2026-01-01T00:00:00Z','sent',7,'right','CL');
    """)
    raw.commit()
    raw.close()
    db.init_db(config)
    conn = db.get_connection(config)
    row = conn.execute("SELECT * FROM applications").fetchone()
    assert (row["channel"], row["run_id"], row["pilot_verdict"], row["cover_letter"]) == ("agent:x", 7, "right", "CL")
    assert row["cv_version"] is None
    conn.execute("INSERT INTO applications (job_id, channel, applied_at, cv_version) VALUES (1, 'pc:company', 'x', 'v')")
    conn.execute("INSERT INTO applications (job_id, channel, applied_at) VALUES (1, 'generic:a.com', 'x')")
    assert "pc_attempts" in [r["name"] for r in conn.execute("PRAGMA table_info(jobs)")]
    db.init_db(config)


def test_pages_render_pc_row(env, with_password=None):
    from tests.conftest import get_csrf_token  # noqa: F401

    client, config, _ = env
    from autojob import auth
    auth.set_password(config, "pw-pw-pw-pw-1")
    jid = _job(config, 1)
    client.get("/api/apply-queue", headers=AUTH)
    _post(client, id=jid, status="submitted", cv_version="cv.pdf@5", cover_letter_used="Dear x")
    token = get_csrf_token(client)
    client.post("/login", data={"password": "pw-pw-pw-pw-1", "csrf_token": token})
    runs = client.get("/runs")
    assert runs.status_code == 200 and "Apply queue" in runs.text and "Role1" in runs.text and "Last pull:" in runs.text
    page = client.get("/")
    assert page.status_code == 200 and "PC</span> linkedin" in page.text and "cv.pdf@5" in page.text
    conn = db.get_connection(config)
    app_id = conn.execute("SELECT id FROM applications").fetchone()[0]
    assert "CV version: cv.pdf@5" in client.get(f"/applications/{app_id}").text


def test_stale_open_items_do_not_block(env):
    client, config, _ = env
    jid = _job(config, 1)
    client.get("/api/apply-queue", headers=AUTH)
    conn = db.get_connection(config)
    assert db.open_batch_job_ids(conn) == {jid}
    conn.execute("UPDATE apply_batches SET day = '2000-01-01'")
    conn.commit()
    assert db.open_batch_job_ids(conn) == set()
    assert remote.claim_next(conn, config, "pc") is not None


def test_bad_timestamp_is_422_and_size_is_stored(env):
    client, config, _ = env
    jid = _job(config, 1)
    batch = client.get("/api/apply-queue", headers=AUTH).json()
    assert batch["size"] == 1
    db.set_setting(db.get_connection(config), "apply_queue_size", 7)
    assert client.get("/api/apply-queue", headers=AUTH).json()["size"] == 1
    for ts in ("0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"):
        assert _post(client, id=jid, status="failed", timestamp=ts).status_code == 422


def test_login_exemption_is_exact(env):
    client, _, _ = env
    r = client.get("/api/apply-queue-x", follow_redirects=False)
    assert r.status_code in (303, 307) and "/login" in r.headers["location"]

"""run_scan orchestration tests. No network, no claude CLI, and no real
browser: every source, every LLM call, and the apply phase are all fakes."""

from __future__ import annotations

import fcntl

import pytest

from autojob import db
from autojob.llm import filter as llm_filter
from autojob.llm import research as llm_research
from autojob.pipeline import run as run_module
from autojob.pipeline.lock import RunInProgress
from autojob.sources import SourceError, alljobs, jobmaster, linkedin


def _job(source="linkedin", n=1, title="Penetration Tester", company="Acme"):
    return {
        "source": source,
        "url": f"https://example.com/{source}/{n}",
        "title": title,
        "company": company,
        "location": "Tel Aviv, Israel",
        "description": "Do offensive security things.",
        "posted_at": None,
        "external_id": str(n),
    }


def _patch_sources(monkeypatch, linkedin_jobs=None, alljobs_jobs=None, jobmaster_jobs=None):
    monkeypatch.setattr(linkedin, "search", lambda keyword, **kw: list(linkedin_jobs or []))
    monkeypatch.setattr(alljobs, "search", lambda keyword: list(alljobs_jobs or []))
    monkeypatch.setattr(jobmaster, "search", lambda keyword: list(jobmaster_jobs or []))


def _patch_no_op_research(monkeypatch):
    monkeypatch.setattr(
        llm_research,
        "research_company",
        lambda name, title, *, work_dir, **kw: {
            "website": None, "tldr": "unused", "careers_url": None,
            "job_url_on_company_site": None, "general_cv_url": None,
            "cv_email": None, "cv_email_source_url": None,
        },
    )


@pytest.fixture(autouse=True)
def _never_call_real_research(monkeypatch):
    """Research runs on every scan now, even with nothing new found, so any
    test that leaves a job queued would otherwise reach the real claude CLI.
    Tests that care about research override this with their own fake."""
    _patch_no_op_research(monkeypatch)


def _patch_no_op_apply(monkeypatch, calls=None):
    """Stand in for the real apply_to_job, which would otherwise launch a
    real browser for every queued job. Records calls (job, auto_submit) if
    a list is given, and never touches the database, so job/application
    status stays exactly what the filter phase set it to."""
    def fake_apply_to_job(job, *, auto_submit, run_id=None):
        if calls is not None:
            calls.append((job, auto_submit))
        return {"status": "ready_to_submit", "application_id": None, "reason": None}

    monkeypatch.setattr(run_module, "apply_to_job", fake_apply_to_job)


def test_run_scan_with_no_targets_logs_and_finishes(config, monkeypatch):
    _patch_sources(monkeypatch)
    run_id = run_module.run_scan(trigger="manual", keywords=[])
    conn = db.get_connection(config)
    try:
        run_row = db.get_run(conn, run_id)
    finally:
        conn.close()
    assert run_row["finished_at"] is not None
    assert run_row["found"] == 0
    assert "No target titles/keywords configured" in run_row["log"]


def test_run_scan_creates_queued_and_filtered_jobs_and_researches_company(config, monkeypatch):
    kept = _job(n=1, title="Penetration Tester", company="Acme")
    dropped = _job(n=2, title="VP of Sales", company="Beta")
    _patch_sources(monkeypatch, linkedin_jobs=[kept, dropped])

    def fake_filter(jobs, cv_text, *, work_dir, batch_size=10, **kwargs):
        results = []
        for job in jobs:
            if job["title"] == "Penetration Tester":
                results.append({"status": "queued", "fit_score": 8.0, "reason": "good fit"})
            else:
                results.append({"status": "filtered_out", "fit_score": 1.0, "reason": "not security"})
        return results

    monkeypatch.setattr(llm_filter, "filter_jobs", fake_filter)

    researched = []

    def fake_research(name, title, *, work_dir, **kw):
        researched.append(name)
        return {
            "website": "https://acme.example", "tldr": "Acme does security consulting.",
            "careers_url": None, "job_url_on_company_site": None, "general_cv_url": None,
            "cv_email": None, "cv_email_source_url": None,
        }

    monkeypatch.setattr(llm_research, "research_company", fake_research)
    _patch_no_op_apply(monkeypatch)

    run_id = run_module.run_scan(trigger="manual", keywords=["pentest"])

    conn = db.get_connection(config)
    try:
        run_row = db.get_run(conn, run_id)
        jobs = db.list_jobs_with_company(conn)
    finally:
        conn.close()

    assert run_row["found"] == 2
    assert run_row["filtered"] == 1
    assert "queued" in run_row["log"]

    by_title = {j["title"]: j for j in jobs}
    assert by_title["Penetration Tester"]["status"] == "queued"
    assert by_title["Penetration Tester"]["fit_score"] == 8.0
    assert by_title["Penetration Tester"]["reason"] == "good fit"
    assert by_title["Penetration Tester"]["company_tldr"] == "Acme does security consulting."
    assert by_title["VP of Sales"]["status"] == "filtered_out"
    assert researched == ["Acme"]


def test_run_scan_stamps_channels_and_reresearches_unstamped_company(config, monkeypatch):
    # A brand new job (Acme) gets researched like any other. A second
    # company (Beta) was already queued from an earlier run and already has
    # a tldr, but never got a channels_researched_at stamp (pre-feature
    # data): it must be researched again too, in the same run.
    kept = _job(n=1, title="Penetration Tester", company="Acme")
    _patch_sources(monkeypatch, linkedin_jobs=[kept])
    monkeypatch.setattr(
        llm_filter, "filter_jobs",
        lambda jobs, cv_text, *, work_dir, batch_size=10, **kwargs: [
            {"status": "queued", "fit_score": 8.0, "reason": "good fit"} for _ in jobs
        ],
    )

    db.init_db(config)
    conn = db.get_connection(config)
    try:
        beta_id = db.get_or_create_company(conn, "Beta")
        conn.execute("UPDATE companies SET tldr = 'Old tldr, no stamp.' WHERE id = ?", (beta_id,))
        conn.commit()
        db.create_job(
            conn, source="linkedin", url="https://example.com/beta/1", title="Pentester",
            company_id=beta_id, status="queued", dedup_key=db.normalize_dedup_key("Beta", "Pentester"),
        )
    finally:
        conn.close()

    researched = []

    def fake_research(name, title, *, work_dir, job_url=""):
        researched.append(name)
        return {
            "website": None, "tldr": f"Researched {name}.", "careers_url": None,
            "job_url_on_company_site": f"{job_url}/on-site" if job_url else None,
            "general_cv_url": None, "cv_email": None, "cv_email_source_url": None,
        }

    monkeypatch.setattr(llm_research, "research_company", fake_research)
    _patch_no_op_apply(monkeypatch)

    run_module.run_scan(trigger="manual", keywords=["pentest"])

    conn = db.get_connection(config)
    try:
        jobs = db.list_jobs_with_company(conn)
        beta = db.get_company(conn, beta_id)
    finally:
        conn.close()

    assert sorted(researched) == ["Acme", "Beta"]
    assert beta["channels_researched_at"] is not None
    by_title_and_company = {(j["title"], j["company_name"]): j for j in jobs}
    acme_job = by_title_and_company[("Penetration Tester", "Acme")]
    assert acme_job["company_site_url"] == f"{acme_job['url']}/on-site"


def test_run_scan_does_not_resurface_already_seen_jobs(config, monkeypatch):
    kept = _job(n=1, title="Penetration Tester", company="Acme")
    _patch_sources(monkeypatch, linkedin_jobs=[kept])
    monkeypatch.setattr(
        llm_filter,
        "filter_jobs",
        lambda jobs, cv_text, *, work_dir, batch_size=10, **kwargs: [
            {"status": "queued", "fit_score": 7.0, "reason": "ok"} for _ in jobs
        ],
    )
    _patch_no_op_research(monkeypatch)
    _patch_no_op_apply(monkeypatch)

    first_run_id = run_module.run_scan(trigger="manual", keywords=["pentest"])
    second_run_id = run_module.run_scan(trigger="manual", keywords=["pentest"])

    conn = db.get_connection(config)
    try:
        first_run = db.get_run(conn, first_run_id)
        second_run = db.get_run(conn, second_run_id)
        all_jobs = db.list_jobs(conn)
    finally:
        conn.close()

    assert first_run["found"] == 1
    assert second_run["found"] == 0
    assert len(all_jobs) == 1


def test_run_scan_never_resurfaces_an_applied_job(config, monkeypatch):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        db.create_job(
            conn,
            source="manual",
            url="https://example.com/already-applied",
            title="Penetration Tester",
            company_id=company_id,
            status="applied",
            dedup_key=db.normalize_dedup_key("Acme", "Penetration Tester"),
        )
    finally:
        conn.close()

    same_job = _job(n=1, title="Penetration Tester", company="Acme")
    same_job["url"] = "https://example.com/found-again-elsewhere"
    _patch_sources(monkeypatch, linkedin_jobs=[same_job])
    monkeypatch.setattr(
        llm_filter,
        "filter_jobs",
        lambda jobs, cv_text, *, work_dir, batch_size=10, **kwargs: [
            {"status": "queued", "fit_score": 7.0, "reason": "ok"} for _ in jobs
        ],
    )
    _patch_no_op_research(monkeypatch)
    _patch_no_op_apply(monkeypatch)

    run_id = run_module.run_scan(trigger="manual", keywords=["pentest"])
    conn = db.get_connection(config)
    try:
        run_row = db.get_run(conn, run_id)
        jobs = db.list_jobs(conn)
    finally:
        conn.close()
    assert run_row["found"] == 0
    assert len(jobs) == 1
    assert jobs[0]["status"] == "applied"


def test_run_scan_logs_blocked_source_and_continues(config, monkeypatch):
    good_job = _job(source="jobmaster", n=1, title="Red Team Operator", company="Delta")

    def raising_alljobs(keyword):
        raise SourceError("alljobs: guest free-text search ignores the keyword")

    monkeypatch.setattr(linkedin, "search", lambda keyword, **kw: [])
    monkeypatch.setattr(alljobs, "search", raising_alljobs)
    monkeypatch.setattr(jobmaster, "search", lambda keyword: [good_job])
    monkeypatch.setattr(
        llm_filter,
        "filter_jobs",
        lambda jobs, cv_text, *, work_dir, batch_size=10, **kwargs: [
            {"status": "queued", "fit_score": 6.0, "reason": "ok"} for _ in jobs
        ],
    )
    _patch_no_op_research(monkeypatch)
    _patch_no_op_apply(monkeypatch)

    run_id = run_module.run_scan(trigger="manual", keywords=["red team"])
    conn = db.get_connection(config)
    try:
        run_row = db.get_run(conn, run_id)
    finally:
        conn.close()
    assert "alljobs" in run_row["log"]
    assert "ignores the keyword" in run_row["log"]
    assert run_row["found"] == 1


def test_run_scan_filter_failure_leaves_job_new_not_queued(config, monkeypatch):
    job = _job(n=1, title="Penetration Tester", company="Acme")
    _patch_sources(monkeypatch, linkedin_jobs=[job])
    monkeypatch.setattr(
        llm_filter,
        "filter_jobs",
        lambda jobs, cv_text, *, work_dir, batch_size=10, **kwargs: [
            {"status": "new", "fit_score": None, "reason": "filter failed: timeout"} for _ in jobs
        ],
    )
    _patch_no_op_research(monkeypatch)
    _patch_no_op_apply(monkeypatch)

    run_id = run_module.run_scan(trigger="manual", keywords=["pentest"])
    conn = db.get_connection(config)
    try:
        run_row = db.get_run(conn, run_id)
        jobs = db.list_jobs(conn)
    finally:
        conn.close()
    assert jobs[0]["status"] == "new"
    assert "filter failed" in jobs[0]["reason"]
    assert run_row["filtered"] == 0


def test_run_scan_respects_research_cap(config, monkeypatch):
    jobs_in = [_job(n=i, title="Penetration Tester", company=f"Company{i}") for i in range(3)]
    _patch_sources(monkeypatch, linkedin_jobs=jobs_in)
    monkeypatch.setattr(
        llm_filter,
        "filter_jobs",
        lambda jobs, cv_text, *, work_dir, batch_size=10, **kwargs: [
            {"status": "queued", "fit_score": 5.0, "reason": "ok"} for _ in jobs
        ],
    )
    researched = []

    def fake_research(name, title, *, work_dir, **kw):
        researched.append(name)
        return {
            "website": None, "tldr": "some tldr", "careers_url": None,
            "job_url_on_company_site": None, "general_cv_url": None,
            "cv_email": None, "cv_email_source_url": None,
        }

    monkeypatch.setattr(llm_research, "research_company", fake_research)
    _patch_no_op_apply(monkeypatch)

    run_id = run_module.run_scan(trigger="manual", keywords=["pentest"], max_research=1)
    conn = db.get_connection(config)
    try:
        run_row = db.get_run(conn, run_id)
    finally:
        conn.close()
    assert len(researched) == 1
    assert "research cap" in run_row["log"]


def test_run_scan_one_bad_source_does_not_crash_the_run(config, monkeypatch):
    def exploding(keyword, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(linkedin, "search", exploding)
    monkeypatch.setattr(alljobs, "search", lambda keyword: [])
    monkeypatch.setattr(jobmaster, "search", lambda keyword: [])

    run_id = run_module.run_scan(trigger="manual", keywords=["pentest"])
    conn = db.get_connection(config)
    try:
        run_row = db.get_run(conn, run_id)
    finally:
        conn.close()
    assert run_row["finished_at"] is not None
    assert run_row["failed"] >= 1


# --- apply phase --------------------------------------------------------
# These seed already-queued jobs straight into the database (as if an
# earlier scan had queued them) and run with keywords=[] so the scan/filter/
# research portion of run_scan is a no-op: only the apply phase runs.

def _seed_queued_job(config, *, source="linkedin", n=1, title="Penetration Tester", company="Acme"):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, f"{company}{n}")
        return db.create_job(
            conn,
            source=source,
            url=f"https://example.com/{source}/{n}",
            title=title,
            company_id=company_id,
            status="queued",
            dedup_key=db.normalize_dedup_key(f"{company}{n}", title),
        )
    finally:
        conn.close()


def test_apply_phase_respects_daily_cap_per_source(config, monkeypatch):
    for n in range(3):
        _seed_queued_job(config, source="linkedin", n=n)
    conn = db.get_connection(config)
    try:
        db.set_setting(conn, "targets", {"daily_cap_per_source": 2})
    finally:
        conn.close()

    calls = []
    _patch_no_op_apply(monkeypatch, calls=calls)

    run_module.run_scan(trigger="manual", keywords=[])

    assert len(calls) == 2


def test_apply_phase_defaults_auto_submit_to_false(config, monkeypatch):
    _seed_queued_job(config)
    calls = []
    _patch_no_op_apply(monkeypatch, calls=calls)

    run_module.run_scan(trigger="manual", keywords=[])

    assert len(calls) == 1
    _, auto_submit = calls[0]
    assert auto_submit is False


def test_apply_phase_honors_auto_submit_setting_when_enabled(config, monkeypatch):
    _seed_queued_job(config)
    conn = db.get_connection(config)
    try:
        db.set_setting(conn, "auto_submit", True)
    finally:
        conn.close()

    calls = []
    _patch_no_op_apply(monkeypatch, calls=calls)

    run_module.run_scan(trigger="manual", keywords=[])

    assert len(calls) == 1
    _, auto_submit = calls[0]
    assert auto_submit is True


def test_apply_phase_leaves_queued_jobs_alone_when_nothing_queued(config, monkeypatch):
    calls = []
    _patch_no_op_apply(monkeypatch, calls=calls)

    run_id = run_module.run_scan(trigger="manual", keywords=[])

    conn = db.get_connection(config)
    try:
        run_row = db.get_run(conn, run_id)
    finally:
        conn.close()
    assert calls == []
    assert run_row["finished_at"] is not None


# --- live view: run_events and progress -------------------------------

def test_run_scan_writes_run_events_for_the_live_view(config, monkeypatch):
    kept = _job(n=1, title="Penetration Tester", company="Acme")
    _patch_sources(monkeypatch, linkedin_jobs=[kept])
    monkeypatch.setattr(
        llm_filter, "filter_jobs",
        lambda jobs, cv_text, *, work_dir, batch_size=10, **kwargs: [
            {"status": "queued", "fit_score": 8.0, "reason": "good fit"} for _ in jobs
        ],
    )
    _patch_no_op_research(monkeypatch)
    _patch_no_op_apply(monkeypatch)

    run_id = run_module.run_scan(trigger="manual", keywords=["pentest"])

    conn = db.get_connection(config)
    try:
        rows = db.list_run_events_since(conn, run_id, 0)
        run_row = db.get_run(conn, run_id)
    finally:
        conn.close()

    assert rows, "expected run_scan to write at least one run_events row"
    kinds = {row["kind"] for row in rows}
    phases = {row["phase"] for row in rows}
    assert "progress" in kinds
    assert {"scan", "filter", "apply", "system"}.issubset(phases)
    assert rows[-1]["message"] == "Run finished."
    assert run_row["progress"] is not None


def test_failed_research_keeps_existing_fields_and_is_retried(config, monkeypatch):
    """A failed research call must not overwrite a good tldr or careers URL,
    must not stamp channels_researched_at (so the next run retries), and
    counts as a failure on the run."""
    _patch_sources(monkeypatch)
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        conn.execute(
            "UPDATE companies SET tldr = 'Good tldr.', careers_url = 'https://acme.example/careers' WHERE id = ?",
            (company_id,),
        )
        conn.commit()
        db.create_job(
            conn, source="linkedin", url="https://example.com/acme/1", title="Pentester",
            company_id=company_id, status="queued", dedup_key="acme-pentester",
        )
    finally:
        conn.close()

    researched = []

    def failing_research(name, title, *, work_dir, **kw):
        researched.append(name)
        return {
            "website": None, "tldr": "unknown (company research failed: timeout)", "careers_url": None,
            "job_url_on_company_site": None, "general_cv_url": None,
            "cv_email": None, "cv_email_source_url": None, "failed": True,
        }

    monkeypatch.setattr(llm_research, "research_company", failing_research)
    _patch_no_op_apply(monkeypatch)

    # Nothing new is found: research must still run for the existing company.
    run_id = run_module.run_scan(trigger="manual", keywords=["pentest"])

    conn = db.get_connection(config)
    try:
        company = db.get_company(conn, company_id)
        run_row = db.get_run(conn, run_id)
    finally:
        conn.close()
    assert researched == ["Acme"]
    assert company["tldr"] == "Good tldr."
    assert company["careers_url"] == "https://acme.example/careers"
    assert company["channels_researched_at"] is None
    assert run_row["failed"] == 1


def test_research_runs_every_scan_even_with_nothing_new(config, monkeypatch):
    _patch_sources(monkeypatch)
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Beta")
        db.create_job(
            conn, source="linkedin", url="https://example.com/beta/1", title="Pentester",
            company_id=company_id, status="queued", dedup_key="beta-pentester",
        )
    finally:
        conn.close()
    researched = []

    def fake_research(name, title, *, work_dir, **kw):
        researched.append(name)
        return {
            "website": None, "tldr": "Beta tldr.", "careers_url": None,
            "job_url_on_company_site": None, "general_cv_url": None,
            "cv_email": None, "cv_email_source_url": None,
        }

    monkeypatch.setattr(llm_research, "research_company", fake_research)
    _patch_no_op_apply(monkeypatch)

    run_module.run_scan(trigger="manual", keywords=["pentest"])

    conn = db.get_connection(config)
    try:
        company = db.get_company(conn, company_id)
    finally:
        conn.close()
    assert researched == ["Beta"]
    assert company["channels_researched_at"] is not None


def _hold_lock(config):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(config.run_lock_path, "a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_refresh_mode_skips_research_and_apply(config, monkeypatch):
    _seed_queued_job(config)
    research_calls, apply_calls = [], []
    monkeypatch.setattr(
        run_module, "research_pending", lambda *a, **kw: research_calls.append(1) or (0, 0)
    )
    _patch_no_op_apply(monkeypatch, calls=apply_calls)

    run_id = run_module.run_scan(trigger="manual", mode="refresh", keywords=[])

    assert research_calls == [] and apply_calls == []
    conn = db.get_connection(config)
    try:
        run = db.get_run(conn, run_id)
        assert run["mode"] == "refresh"
        assert "Refresh: research and apply skipped." in run["log"]
    finally:
        conn.close()


def test_run_scan_rejects_unknown_mode(config):
    with pytest.raises(ValueError):
        run_module.run_scan(mode="bogus", keywords=[])


def test_run_scan_raises_when_lock_held(config):
    handle = _hold_lock(config)
    try:
        with pytest.raises(RunInProgress):
            run_module.run_scan(keywords=[])
    finally:
        handle.close()
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        assert db.list_runs(conn) == []
    finally:
        conn.close()


def test_run_scan_closes_interrupted_runs_and_requeues_applying_jobs(config, monkeypatch):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        old_run = db.create_run(conn, trigger="manual")
        job_ids = {}
        for name, app_status in (("a", "needs_help"), ("b", "ready_to_submit")):
            company_id = db.get_or_create_company(conn, name)
            job_id = db.create_job(
                conn, source="linkedin", url=f"https://example.com/{name}", title="Pentester",
                company_id=company_id, status="applying", dedup_key=db.normalize_dedup_key(name, "Pentester"),
            )
            db.create_application(conn, job_id=job_id, channel="other", status=app_status)
            job_ids[name] = job_id
    finally:
        conn.close()
    _patch_no_op_apply(monkeypatch)

    run_module.run_scan(keywords=[])

    conn = db.get_connection(config)
    try:
        old = db.get_run(conn, old_run)
        assert old["finished_at"] is not None
        assert "Interrupted:" in old["log"]
        assert db.get_job(conn, job_ids["a"])["status"] == "queued"
        assert db.get_job(conn, job_ids["b"])["status"] == "applying"
    finally:
        conn.close()


def test_scheduled_run_never_enables_auto_submit(config, monkeypatch):
    _patch_sources(monkeypatch)
    _seed_queued_job(config)
    calls = []
    _patch_no_op_apply(monkeypatch, calls=calls)

    assert run_module.main() == 0

    assert calls[0][1] is False
    conn = db.get_connection(config)
    try:
        assert db.get_setting(conn, "auto_submit") is None
    finally:
        conn.close()


def test_main_skips_when_lock_held(config):
    handle = _hold_lock(config)
    try:
        assert run_module.main() == 0
    finally:
        handle.close()
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        assert db.list_runs(conn) == []
    finally:
        conn.close()

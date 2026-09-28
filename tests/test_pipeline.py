"""run_scan orchestration tests. No network, no claude CLI, and no real
browser: every source, every LLM call, and the apply phase are all fakes."""

from __future__ import annotations

from autojob import db
from autojob.llm import filter as llm_filter
from autojob.llm import research as llm_research
from autojob.pipeline import run as run_module
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
        lambda name, title, *, work_dir: {"website": None, "tldr": "unused", "careers_url": None},
    )


def _patch_no_op_apply(monkeypatch, calls=None):
    """Stand in for the real apply_to_job, which would otherwise launch a
    real browser for every queued job. Records calls (job, auto_submit) if
    a list is given, and never touches the database, so job/application
    status stays exactly what the filter phase set it to."""
    def fake_apply_to_job(job, *, auto_submit):
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

    def fake_filter(jobs, cv_text, *, work_dir, batch_size=10):
        results = []
        for job in jobs:
            if job["title"] == "Penetration Tester":
                results.append({"status": "queued", "fit_score": 8.0, "reason": "good fit"})
            else:
                results.append({"status": "filtered_out", "fit_score": 1.0, "reason": "not security"})
        return results

    monkeypatch.setattr(llm_filter, "filter_jobs", fake_filter)

    researched = []

    def fake_research(name, title, *, work_dir):
        researched.append(name)
        return {"website": "https://acme.example", "tldr": "Acme does security consulting.", "careers_url": None}

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


def test_run_scan_does_not_resurface_already_seen_jobs(config, monkeypatch):
    kept = _job(n=1, title="Penetration Tester", company="Acme")
    _patch_sources(monkeypatch, linkedin_jobs=[kept])
    monkeypatch.setattr(
        llm_filter,
        "filter_jobs",
        lambda jobs, cv_text, *, work_dir, batch_size=10: [
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
        lambda jobs, cv_text, *, work_dir, batch_size=10: [
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
        lambda jobs, cv_text, *, work_dir, batch_size=10: [
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
        lambda jobs, cv_text, *, work_dir, batch_size=10: [
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
        lambda jobs, cv_text, *, work_dir, batch_size=10: [
            {"status": "queued", "fit_score": 5.0, "reason": "ok"} for _ in jobs
        ],
    )
    researched = []

    def fake_research(name, title, *, work_dir):
        researched.append(name)
        return {"website": None, "tldr": "some tldr", "careers_url": None}

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

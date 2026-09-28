"""LLM wrapper tests. No real subprocess calls: `claude` is always mocked."""

from __future__ import annotations

import json
import subprocess

import pytest

from autojob.llm import filter as llm_filter
from autojob.llm import research as llm_research
from autojob.llm.cli import ClaudeCliError, extract_json, run_claude


class FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _fake_run(result_text: str):
    def _run(cmd, input, capture_output, text, cwd, timeout):  # noqa: A002
        return FakeCompleted(json.dumps({"result": result_text}))

    return _run


# --- extract_json ---------------------------------------------------------

def test_extract_json_plain_array():
    assert extract_json('[{"a": 1}]') == [{"a": 1}]


def test_extract_json_with_markdown_fence():
    text = "Here you go:\n```json\n{\"a\": 1}\n```\nDone."
    assert extract_json(text) == {"a": 1}


def test_extract_json_raises_when_no_json_present():
    with pytest.raises(ClaudeCliError):
        extract_json("no json in here at all")


# --- run_claude -------------------------------------------------------

def test_run_claude_uses_throwaway_cwd_and_returns_result(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, input, capture_output, text, cwd, timeout):  # noqa: A002
        captured["cwd"] = cwd
        captured["input"] = input
        return FakeCompleted(json.dumps({"result": "hello"}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    work_dir = tmp_path / "llm-work"
    result = run_claude("prompt text", model="haiku", cwd=work_dir, timeout=30)
    assert result == "hello"
    assert captured["cwd"] == str(work_dir)
    assert captured["input"] == "prompt text"
    assert work_dir.exists()


def test_run_claude_raises_on_timeout(monkeypatch, tmp_path):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ClaudeCliError):
        run_claude("x", model="haiku", cwd=tmp_path, timeout=30)


def test_run_claude_raises_on_nonzero_exit(monkeypatch, tmp_path):
    def fake_run(*a, **k):
        return FakeCompleted("", returncode=1, stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ClaudeCliError):
        run_claude("x", model="haiku", cwd=tmp_path, timeout=30)


# --- filter_jobs --------------------------------------------------------

def test_filter_jobs_maps_keep_and_drop(monkeypatch, tmp_path):
    # Both titles pass the deterministic title screen, so both reach the LLM.
    jobs = [
        {"title": "Penetration Tester", "company": "A", "location": "Israel", "description": "pentest"},
        {"title": "Security Researcher", "company": "B", "location": "Israel", "description": "research"},
    ]
    response = [
        {"fit_score": 8, "keep": True, "reason": "good fit"},
        {"fit_score": 2, "keep": False, "reason": "too senior"},
    ]
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps(response)))
    results = llm_filter.filter_jobs(jobs, "Sean's CV text", work_dir=tmp_path)
    assert results[0] == {"status": "queued", "fit_score": 8.0, "reason": "good fit"}
    assert results[1] == {"status": "filtered_out", "fit_score": 2.0, "reason": "too senior"}


def test_filter_jobs_title_screen_drops_before_llm(monkeypatch, tmp_path):
    # A lawyer and a CTO must be dropped by the title screen without any CLI call,
    # while the real security role in the same batch is still scored by the LLM.
    calls = {"n": 0}

    def counting_run(prompt, **kwargs):
        calls["n"] += 1
        return json.dumps([{"fit_score": 9, "keep": True, "reason": "strong"}])

    monkeypatch.setattr(llm_filter, "run_claude", counting_run)
    jobs = [
        {"title": "Corporate Lawyer", "company": "A", "location": "Israel", "description": ""},
        {"title": "CTO", "company": "B", "location": "Israel", "description": ""},
        {"title": "Junior Penetration Tester", "company": "C", "location": "Israel", "description": ""},
    ]
    results = llm_filter.filter_jobs(jobs, "cv", work_dir=tmp_path)
    assert results[0]["status"] == "filtered_out" and "title screen" in results[0]["reason"]
    assert results[1]["status"] == "filtered_out" and "title screen" in results[1]["reason"]
    assert results[2] == {"status": "queued", "fit_score": 9.0, "reason": "strong"}
    assert calls["n"] == 1  # only the one non-blocked job reached the LLM


def test_filter_jobs_marks_new_with_reason_on_cli_failure(monkeypatch, tmp_path):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=180)

    monkeypatch.setattr(subprocess, "run", fake_run)
    jobs = [{"title": "X", "company": "Y", "location": "Israel", "description": ""}]
    results = llm_filter.filter_jobs(jobs, "cv", work_dir=tmp_path)
    assert results[0]["status"] == "new"
    assert "filter failed" in results[0]["reason"]
    assert results[0]["fit_score"] is None


def test_filter_jobs_marks_new_on_malformed_response(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_run("not json at all"))
    jobs = [{"title": "X", "company": "Y", "location": "Israel", "description": ""}]
    results = llm_filter.filter_jobs(jobs, "cv", work_dir=tmp_path)
    assert results[0]["status"] == "new"
    assert "filter failed" in results[0]["reason"]


def test_filter_jobs_batches_by_batch_size(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, input, capture_output, text, cwd, timeout):  # noqa: A002
        calls.append(input)
        n = input.count("Job ")
        response = [{"fit_score": 5, "keep": True, "reason": "ok"} for _ in range(n)]
        return FakeCompleted(json.dumps({"result": json.dumps(response)}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    jobs = [{"title": f"T{i}", "company": "C", "location": "Israel", "description": ""} for i in range(15)]
    results = llm_filter.filter_jobs(jobs, "cv", work_dir=tmp_path, batch_size=10)
    assert len(results) == 15
    assert len(calls) == 2


# --- filter_jobs live events (the run_events bridge) -----------------------

def test_filter_jobs_emits_score_and_progress_events_with_a_fake_llm(config, monkeypatch, tmp_path):
    """A fake LLM response (no real CLI call) run through the real
    filter_jobs, with a real conn/run_id, must emit at least one "score"
    event and at least one "progress" event through the events helper."""
    from autojob import db
    from autojob.pipeline import events as events_module

    calls = []
    monkeypatch.setattr(events_module, "emit", lambda *a, **k: calls.append((a, k)))

    def fake_run(cmd, input, capture_output, text, cwd, timeout):  # noqa: A002
        response = [{"fit_score": 8, "keep": True, "reason": "great fit"}]
        return FakeCompleted(json.dumps({"result": json.dumps(response)}))

    monkeypatch.setattr(subprocess, "run", fake_run)

    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        jobs = [{"title": "Penetration Tester", "company": "Acme", "location": "Israel", "description": ""}]
        llm_filter.filter_jobs(jobs, "cv", work_dir=tmp_path, conn=conn, run_id=run_id)
    finally:
        conn.close()

    kinds = {call[0][3] for call in calls}  # emit(conn, run_id, phase, kind, message, detail=None)
    assert "score" in kinds
    assert "progress" in kinds
    score_messages = [call[0][4] for call in calls if call[0][3] == "score"]
    assert any("Penetration Tester @ Acme" in m for m in score_messages)


def test_filter_jobs_title_screen_drop_emits_an_info_event(config, monkeypatch, tmp_path):
    from autojob import db
    from autojob.pipeline import events as events_module

    calls = []
    monkeypatch.setattr(events_module, "emit", lambda *a, **k: calls.append(a))

    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
        jobs = [{"title": "Corporate Lawyer", "company": "Acme", "location": "Israel", "description": ""}]
        llm_filter.filter_jobs(jobs, "cv", work_dir=tmp_path, conn=conn, run_id=run_id)
    finally:
        conn.close()

    messages = [call[4] for call in calls]
    assert any("title screen drop - Corporate Lawyer" in m for m in messages)


def test_filter_jobs_without_conn_or_run_id_emits_nothing(monkeypatch, tmp_path):
    """The direct-call path every other filter test uses (no conn/run_id)
    must stay silent: filter_jobs is also called from places with no run in
    progress (a future dry-run tool, tests), and it must not raise or write
    events for those."""
    from autojob.pipeline import events as events_module

    calls = []
    monkeypatch.setattr(events_module, "emit", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps([{"fit_score": 5, "keep": True, "reason": "ok"}])))

    jobs = [{"title": "Penetration Tester", "company": "Acme", "location": "Israel", "description": ""}]
    llm_filter.filter_jobs(jobs, "cv", work_dir=tmp_path)

    assert calls == []


# --- research_company -------------------------------------------------

def test_research_company_returns_parsed_fields(monkeypatch, tmp_path):
    payload = {
        "website": "https://example.com",
        "tldr": "Does security things.",
        "careers_url": "https://example.com/careers",
        "job_url_on_company_site": "https://example.com/jobs/42",
        "general_cv_url": "https://example.com/send-cv",
        "cv_email": "Jobs@Example.com",
        "cv_email_source_url": "https://example.com/contact",
    }
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps(payload)))
    info = llm_research.research_company("ExampleCo", "Pentester", work_dir=tmp_path)
    assert info["website"] == "https://example.com"
    assert info["tldr"] == "Does security things."
    assert info["careers_url"] == "https://example.com/careers"
    assert info["job_url_on_company_site"] == "https://example.com/jobs/42"
    assert info["general_cv_url"] == "https://example.com/send-cv"
    assert info["cv_email"] == "jobs@example.com"
    assert info["cv_email_source_url"] == "https://example.com/contact"


def test_research_company_drops_email_without_source_url(monkeypatch, tmp_path):
    payload = {
        "website": None, "tldr": "unused", "careers_url": None,
        "job_url_on_company_site": None, "general_cv_url": None,
        "cv_email": "jobs@example.com", "cv_email_source_url": None,
    }
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps(payload)))
    info = llm_research.research_company("ExampleCo", "Pentester", work_dir=tmp_path)
    assert info["cv_email"] is None
    assert info["cv_email_source_url"] is None


def test_research_company_drops_malformed_email(monkeypatch, tmp_path):
    payload = {
        "website": None, "tldr": "unused", "careers_url": None,
        "job_url_on_company_site": None, "general_cv_url": None,
        "cv_email": "not-an-email", "cv_email_source_url": "https://example.com/contact",
    }
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps(payload)))
    info = llm_research.research_company("ExampleCo", "Pentester", work_dir=tmp_path)
    assert info["cv_email"] is None


def test_research_company_drops_job_board_url(monkeypatch, tmp_path):
    payload = {
        "website": None, "tldr": "unused", "careers_url": None,
        "job_url_on_company_site": "https://www.linkedin.com/jobs/view/12345",
        "general_cv_url": None, "cv_email": None, "cv_email_source_url": None,
    }
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps(payload)))
    info = llm_research.research_company("ExampleCo", "Pentester", work_dir=tmp_path)
    assert info["job_url_on_company_site"] is None


def test_research_company_returns_unknown_on_failure(monkeypatch, tmp_path):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=300)

    monkeypatch.setattr(subprocess, "run", fake_run)
    info = llm_research.research_company("ExampleCo", "Pentester", work_dir=tmp_path)
    assert info["website"] is None
    assert "unknown" in info["tldr"]
    assert info["careers_url"] is None
    assert info["job_url_on_company_site"] is None
    assert info["general_cv_url"] is None
    assert info["cv_email"] is None
    assert info["cv_email_source_url"] is None


def test_research_company_prompt_has_honest_profile_not_years_of_experience():
    prompt = llm_research._build_prompt("ExampleCo", "Pentester", "https://example.com/job/1")
    assert "close to no professional" in prompt
    assert "2 years" not in prompt


def test_research_company_flags_failure(monkeypatch, tmp_path):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=300)

    monkeypatch.setattr(subprocess, "run", fake_run)
    info = llm_research.research_company("ExampleCo", "Pentester", work_dir=tmp_path)
    assert info["failed"] is True


def test_research_company_success_is_not_flagged_failed(monkeypatch, tmp_path):
    payload = {"website": "https://example.com", "tldr": "Does things."}
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps(payload)))
    info = llm_research.research_company("ExampleCo", "Pentester", work_dir=tmp_path)
    assert info["failed"] is False


@pytest.mark.parametrize(
    "source_url",
    [
        "https://il.indeed.co.il/viewjob?jk=1",
        "https://www.glassdoor.co.uk/job-listing/x",
        "https://www.glassdoor.co.il/Job/x",
        "https://lnkd.in/abc123",
    ],
)
def test_clean_email_rejects_job_board_source_pages(source_url):
    assert llm_research._clean_email("jobs@acme.com", source_url, "https://acme.com") == (None, None)


def test_clean_email_rejects_address_on_an_unrelated_domain():
    email, source = llm_research._clean_email(
        "hr@gmail.com", "https://acme.com/careers", "https://acme.com"
    )
    assert (email, source) == (None, None)


def test_clean_email_accepts_company_domain_and_subdomains():
    assert llm_research._clean_email(
        "jobs@acme.co.il", "https://careers.acme.co.il/contact", "https://www.acme.co.il"
    )[0] == "jobs@acme.co.il"
    assert llm_research._clean_email(
        "cv@hr.acme.co.il", "https://blog.example.org/post", "https://acme.co.il"
    )[0] == "cv@hr.acme.co.il"


def test_clean_email_accepts_source_page_host_when_website_unknown():
    assert llm_research._clean_email("jobs@acme.io", "https://www.acme.io/jobs", None)[0] == "jobs@acme.io"
    assert llm_research._clean_email("jobs@other.io", "https://www.acme.io/jobs", None) == (None, None)


def test_clean_email_rejects_address_on_a_job_board_domain():
    assert llm_research._clean_email(
        "jobs@alljobs.co.il", "https://www.alljobs.co.il/x", None
    ) == (None, None)

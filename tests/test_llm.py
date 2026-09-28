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
    jobs = [
        {"title": "Penetration Tester", "company": "A", "location": "Israel", "description": "pentest"},
        {"title": "Senior Manager", "company": "B", "location": "Israel", "description": "manage"},
    ]
    response = [
        {"fit_score": 8, "keep": True, "reason": "good fit"},
        {"fit_score": 2, "keep": False, "reason": "too senior"},
    ]
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps(response)))
    results = llm_filter.filter_jobs(jobs, "Sean's CV text", work_dir=tmp_path)
    assert results[0] == {"status": "queued", "fit_score": 8.0, "reason": "good fit"}
    assert results[1] == {"status": "filtered_out", "fit_score": 2.0, "reason": "too senior"}


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


# --- research_company -------------------------------------------------

def test_research_company_returns_parsed_fields(monkeypatch, tmp_path):
    payload = {"website": "https://example.com", "tldr": "Does security things.", "careers_url": None}
    monkeypatch.setattr(subprocess, "run", _fake_run(json.dumps(payload)))
    info = llm_research.research_company("ExampleCo", "Pentester", work_dir=tmp_path)
    assert info["website"] == "https://example.com"
    assert info["tldr"] == "Does security things."
    assert info["careers_url"] is None


def test_research_company_returns_unknown_on_failure(monkeypatch, tmp_path):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=300)

    monkeypatch.setattr(subprocess, "run", fake_run)
    info = llm_research.research_company("ExampleCo", "Pentester", work_dir=tmp_path)
    assert info["website"] is None
    assert "unknown" in info["tldr"]

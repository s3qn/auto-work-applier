"""Tests for the top-level apply_to_job orchestrator. No real browser and no
real claude CLI: browser_page, pick_handler, and run_claude are all fakes."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from autojob import db
from autojob.apply import answers as answers_module
from autojob.apply import apply as apply_module
from autojob.browser.context import BrowserBusyError


class FakePage:
    def __init__(self):
        self.goto_calls: list[str] = []

    def goto(self, url, wait_until=None):
        self.goto_calls.append(url)

    def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"fake png")

    def content(self):
        return "<html>confirmed</html>"

    def pdf(self, path):
        Path(path).write_bytes(b"%PDF-1.4 fake")


def _fake_browser_page_factory(page):
    @contextmanager
    def _fake_browser_page(config, headless=True):
        yield page

    return _fake_browser_page


def _fake_handler(result: dict, name: str = "greenhouse"):
    calls = []

    def fake_apply(page, answers, cv_path, auto_submit):
        calls.append({"answers": answers, "cv_path": cv_path, "auto_submit": auto_submit})
        return dict(result)

    return SimpleNamespace(__name__=f"autojob.apply.handlers.{name}", apply=fake_apply), calls


def _patch_claude(monkeypatch, cover_letter="Dear hiring team, honest letter.", screening_answers=None):
    payload = {"cover_letter": cover_letter, "screening_answers": screening_answers or {}}
    monkeypatch.setattr(answers_module, "run_claude", lambda prompt, **kw: json.dumps(payload))


def _seed_job(config, *, careers_url=None):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        if careers_url:
            db.update_company_research(conn, company_id, website=None, tldr="Acme tldr", careers_url=careers_url)
        job_id = db.create_job(
            conn,
            source="manual",
            url="https://job-boards.greenhouse.io/acme/jobs/1",
            title="Junior Penetration Tester",
            company_id=company_id,
            status="queued",
        )
        job = db.get_job_with_company(conn, job_id)
    finally:
        conn.close()
    return job


def test_apply_to_job_holds_as_ready_to_submit_when_auto_submit_false(config, monkeypatch):
    job = _seed_job(config)
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    handler, calls = _fake_handler({"status": "ready_to_submit", "reason": None})
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "ready_to_submit"
    assert calls[0]["auto_submit"] is False  # the safety default actually reached the handler

    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, result["application_id"])
        job_row = db.get_job(conn, job["id"])
        proofs = db.list_proofs(conn, result["application_id"])
    finally:
        conn.close()
    assert application["status"] == "ready_to_submit"
    assert application["channel"] == "greenhouse"
    assert application["cover_letter"] == "Dear hiring team, honest letter."
    assert job_row["status"] == "applying"
    assert [p["kind"] for p in proofs] == ["screenshot"]  # no confirmation without a real submit


def test_apply_to_job_marks_sent_and_captures_confirmation_when_auto_submit_true(config, monkeypatch):
    job = _seed_job(config)
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    handler, calls = _fake_handler({"status": "sent", "reason": None})
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=True)

    assert result["status"] == "sent"
    assert calls[0]["auto_submit"] is True

    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, result["application_id"])
        job_row = db.get_job(conn, job["id"])
        proofs = db.list_proofs(conn, result["application_id"])
    finally:
        conn.close()
    assert application["status"] == "sent"
    assert job_row["status"] == "applied"
    assert set(p["kind"] for p in proofs) == {"screenshot", "confirmation", "pdf"}


def test_apply_to_job_uses_careers_url_when_present(config, monkeypatch):
    job = _seed_job(config, careers_url="https://jobs.lever.co/acme/xyz")
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    handler, calls = _fake_handler({"status": "ready_to_submit", "reason": None})
    picked_urls = []
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: (picked_urls.append(url), handler)[1])
    _patch_claude(monkeypatch)

    apply_module.apply_to_job(job, auto_submit=False)

    assert picked_urls == ["https://jobs.lever.co/acme/xyz"]
    assert page.goto_calls == ["https://jobs.lever.co/acme/xyz"]


def test_apply_to_job_unknown_ats_creates_help_item_and_no_application(config, monkeypatch):
    job = _seed_job(config)
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: None)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result == {"status": "needs_help", "application_id": None, "reason": result["reason"]}
    assert "No apply handler" in result["reason"]

    conn = db.get_connection(config)
    try:
        job_row = db.get_job(conn, job["id"])
        help_items = db.list_open_help_items(conn)
    finally:
        conn.close()
    assert job_row["status"] == "needs_help"
    assert len(help_items) == 1


def test_apply_to_job_field_it_cannot_answer_creates_help_item(config, monkeypatch):
    job = _seed_job(config)
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    handler, _ = _fake_handler(
        {"status": "needs_help", "reason": "Greenhouse form has a required question with no honest answer."}
    )
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "needs_help"
    conn = db.get_connection(config)
    try:
        job_row = db.get_job(conn, job["id"])
        help_items = db.list_open_help_items(conn)
        application = db.get_application(conn, result["application_id"])
    finally:
        conn.close()
    assert job_row["status"] == "needs_help"
    assert len(help_items) == 1
    assert application["status"] == "needs_help"


def test_apply_to_job_emits_events_when_run_id_is_given(config, monkeypatch):
    """A real pipeline run passes its run_id through apply_to_job so the
    live view can show which ATS was picked, the filling step, and the
    final outcome. Monkeypatch the events helper (no real conn needed for
    the fake calls) and check both are emitted, tagged with that run_id."""
    job = _seed_job(config)
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    handler, calls = _fake_handler({"status": "ready_to_submit", "reason": None})
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    _patch_claude(monkeypatch)

    emitted = []
    monkeypatch.setattr(apply_module.events, "emit", lambda *a, **k: emitted.append(a))

    result = apply_module.apply_to_job(job, auto_submit=False, run_id=42)

    assert result["status"] == "ready_to_submit"
    assert emitted, "expected apply_to_job to emit at least one run_events row"
    assert all(call[1] == 42 for call in emitted)  # run_id threaded through every emit call
    messages = [call[4] for call in emitted]  # emit(conn, run_id, phase, kind, message, detail=None)
    assert any("filling" in m for m in messages)
    assert any("ready_to_submit" in m for m in messages)
    assert any("greenhouse" in m for m in messages)


def test_apply_to_job_without_run_id_emits_nothing(config, monkeypatch):
    job = _seed_job(config)
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    handler, calls = _fake_handler({"status": "ready_to_submit", "reason": None})
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    _patch_claude(monkeypatch)

    emitted = []
    monkeypatch.setattr(apply_module.events, "emit", lambda *a, **k: emitted.append(a))

    apply_module.apply_to_job(job, auto_submit=False)

    assert emitted == []


def test_apply_to_job_browser_busy_requeues_the_job(config, monkeypatch):
    job = _seed_job(config)

    @contextmanager
    def raising_browser_page(config, headless=True):
        raise BrowserBusyError("Another browser session is already running. Try again shortly.")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(apply_module, "browser_page", raising_browser_page)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "failed"
    assert result["application_id"] is None
    conn = db.get_connection(config)
    try:
        job_row = db.get_job(conn, job["id"])
    finally:
        conn.close()
    assert job_row["status"] == "queued"

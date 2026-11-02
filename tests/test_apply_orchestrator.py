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
    def _fake_browser_page(config, headless=True, visible=False):
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
    def raising_browser_page(config, headless=True, visible=False):
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


# --- channel chain (company site, careers, general CV, email, board URL) --

def _seed_job_with_channels(
    config,
    *,
    company_site_url=None,
    careers_url=None,
    general_cv_url=None,
    cv_email=None,
    job_url="https://boards.example.com/acme/999",
):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        db.update_company_research(
            conn, company_id, website=None, tldr="Acme tldr", careers_url=careers_url,
            general_cv_url=general_cv_url, cv_email=cv_email,
            cv_email_source_url="https://acme.example.com/careers" if cv_email else None,
        )
        job_id = db.create_job(
            conn, source="manual", url=job_url, title="Junior Penetration Tester",
            company_id=company_id, status="queued",
        )
        if company_site_url:
            db.set_job_company_site_url(conn, job_id, company_site_url)
        job = db.get_job_with_company(conn, job_id)
    finally:
        conn.close()
    return job


def test_plan_channels_order_with_all_fields_set(config):
    job = _seed_job_with_channels(
        config,
        company_site_url="https://acme.example.com/jobs/42",
        careers_url="https://acme.example.com/careers/positions/1234",
        general_cv_url="https://acme.example.com/careers/send-cv",
        cv_email="jobs@acme.example.com",
        job_url="https://boards.example.com/acme/999",
    )
    conn = db.get_connection(config)
    try:
        plan = apply_module.plan_channels(job, conn)
    finally:
        conn.close()
    assert plan == [
        ("company_job", "https://acme.example.com/jobs/42"),
        ("careers", "https://acme.example.com/careers/positions/1234"),
        ("general_cv", "https://acme.example.com/careers/send-cv"),
        ("email", "jobs@acme.example.com"),
        ("job_url", "https://boards.example.com/acme/999"),
    ]


def test_plan_channels_skips_bare_careers_listing(config):
    job = _seed_job_with_channels(config, careers_url="https://acme.example.com/careers")
    conn = db.get_connection(config)
    try:
        plan = apply_module.plan_channels(job, conn)
    finally:
        conn.close()
    assert [kind for kind, _ in plan] == ["job_url"]


def test_plan_channels_keeps_specific_careers_posting(config):
    job = _seed_job_with_channels(config, careers_url="https://acme.example.com/careers/positions/1234")
    conn = db.get_connection(config)
    try:
        plan = apply_module.plan_channels(job, conn)
    finally:
        conn.close()
    assert ("careers", "https://acme.example.com/careers/positions/1234") in plan


def test_plan_channels_drops_email_when_already_sent(config):
    job = _seed_job_with_channels(config, cv_email="jobs@acme.example.com")
    conn = db.get_connection(config)
    try:
        db.create_application(
            conn, job_id=job["id"], channel="email:acme.example.com", status="sent",
            email_to="jobs@acme.example.com",
        )
        plan = apply_module.plan_channels(job, conn)
    finally:
        conn.close()
    assert [kind for kind, _ in plan] == ["job_url"]


def test_plan_channels_drops_email_when_daily_cap_reached(config):
    job = _seed_job_with_channels(config, cv_email="jobs@acme.example.com")
    conn = db.get_connection(config)
    try:
        db.set_setting(conn, "email_daily_cap", 0)
        plan = apply_module.plan_channels(job, conn)
    finally:
        conn.close()
    assert [kind for kind, _ in plan] == ["job_url"]


def test_captcha_on_company_job_falls_through_to_email(config, monkeypatch):
    job = _seed_job_with_channels(
        config,
        company_site_url="https://acme.example.com/jobs/42",
        cv_email="jobs@acme.example.com",
    )
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    handler, _ = _fake_handler({"status": "needs_help", "reason": "CAPTCHA on form."})
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "ready_to_submit"
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, result["application_id"])
    finally:
        conn.close()
    assert application["channel"] == "email:acme.example.com"
    assert application["status"] == "ready_to_submit"
    assert "Tried company_job https://acme.example.com/jobs/42: CAPTCHA on form." in application["notes"]


def test_unanswerable_field_on_company_job_escalates_without_trying_email(config, monkeypatch):
    job = _seed_job_with_channels(
        config,
        company_site_url="https://acme.example.com/jobs/42",
        cv_email="jobs@acme.example.com",
    )
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    handler, _ = _fake_handler(
        {"status": "needs_help", "reason": "Greenhouse form has a required question with no honest answer."}
    )
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    email_calls = []
    monkeypatch.setattr(apply_module.mailer, "draft_email", lambda **kw: email_calls.append(kw) or {})
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "needs_help"
    assert email_calls == []
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, result["application_id"])
    finally:
        conn.close()
    assert not (application["channel"] or "").startswith("email:")


def test_successful_company_form_never_also_emails(config, monkeypatch):
    job = _seed_job_with_channels(
        config,
        company_site_url="https://acme.example.com/jobs/42",
        cv_email="jobs@acme.example.com",
    )
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    handler, _ = _fake_handler({"status": "ready_to_submit", "reason": None})
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    email_calls = []
    monkeypatch.setattr(apply_module.mailer, "draft_email", lambda **kw: email_calls.append(kw) or {})
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "ready_to_submit"
    assert email_calls == []
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, result["application_id"])
    finally:
        conn.close()
    assert not (application["channel"] or "").startswith("email:")


def test_last_channel_failing_still_escalates_needs_help(config, monkeypatch):
    job = _seed_job_with_channels(config)  # nothing researched: plan is just [job_url]
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    handler, _ = _fake_handler({"status": "needs_help", "reason": "CAPTCHA on form."})
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "needs_help"
    conn = db.get_connection(config)
    try:
        help_items = db.list_open_help_items(conn)
    finally:
        conn.close()
    assert len(help_items) == 1


def test_email_channel_auto_submit_false_never_calls_send_draft(config, monkeypatch):
    job = _seed_job_with_channels(config, cv_email="jobs@acme.example.com")
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    send_calls = []
    monkeypatch.setattr(apply_module.mailer, "send_draft", lambda *a, **kw: send_calls.append(a))
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "ready_to_submit"
    assert send_calls == []
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, result["application_id"])
        job_row = db.get_job(conn, job["id"])
    finally:
        conn.close()
    assert application["channel"] == "email:acme.example.com"
    assert application["email_to"] == "jobs@acme.example.com"
    assert json.loads(application["email_draft"])["to"] == "jobs@acme.example.com"
    assert job_row["status"] == "applying"


def test_email_channel_auto_submit_true_sends_and_marks_applied(config, monkeypatch):
    job = _seed_job_with_channels(config, cv_email="jobs@acme.example.com")
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    send_calls = []

    def fake_send_draft(conn, cfg, application_id, draft, cv_path):
        send_calls.append((application_id, draft["to"]))

    monkeypatch.setattr(apply_module.mailer, "send_draft", fake_send_draft)
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=True)

    assert result["status"] == "sent"
    assert len(send_calls) == 1
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, result["application_id"])
        job_row = db.get_job(conn, job["id"])
    finally:
        conn.close()
    assert application["status"] == "sent"
    assert job_row["status"] == "applied"


def test_email_channel_smtp_not_configured_keeps_ready_to_submit_and_creates_help_item(config, monkeypatch):
    job = _seed_job_with_channels(config, cv_email="jobs@acme.example.com")
    page = FakePage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))

    def raising_send_draft(*a, **kw):
        raise apply_module.mailer.SmtpNotConfigured("nope")

    monkeypatch.setattr(apply_module.mailer, "send_draft", raising_send_draft)
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=True)

    assert result["status"] == "ready_to_submit"
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, result["application_id"])
        job_row = db.get_job(conn, job["id"])
        help_items = db.list_open_help_items(conn)
    finally:
        conn.close()
    assert application["status"] == "ready_to_submit"
    assert job_row["status"] == "needs_help"
    assert len(help_items) == 1
    assert "SMTP not configured" in help_items[0]["reason"]


def test_release_email_application_sends_resolves_help_item_no_browser(config, monkeypatch):
    job = _seed_job_with_channels(config, cv_email="jobs@acme.example.com")
    conn = db.get_connection(config)
    try:
        draft = {
            "to": "jobs@acme.example.com", "subject": "Application", "body": "Dear team",
            "attachment_name": None,
        }
        application_id = db.create_application(
            conn, job_id=job["id"], channel="email:acme.example.com", status="ready_to_submit",
            email_to=draft["to"], email_draft=json.dumps(draft),
        )
        db.create_help_item(
            conn, job_id=job["id"],
            reason="Email SMTP not configured. Run: uv run python -m autojob.set_smtp",
        )
        db.update_job_status(conn, job["id"], "needs_help")
    finally:
        conn.close()

    send_calls = []

    def fake_send_draft(conn, cfg, app_id, draft, cv_path):
        send_calls.append(app_id)

    monkeypatch.setattr(apply_module.mailer, "send_draft", fake_send_draft)

    def exploding_browser_page(config, headless=True, visible=False):
        raise AssertionError("release of an email application must not open a browser")

    monkeypatch.setattr(apply_module, "browser_page", exploding_browser_page)

    result = apply_module.release_application(application_id)

    assert result["status"] == "sent"
    assert send_calls == [application_id]
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, application_id)
        job_row = db.get_job(conn, job["id"])
        help_items = db.list_open_help_items(conn)
    finally:
        conn.close()
    assert application["status"] == "sent"
    assert job_row["status"] == "applied"
    assert help_items == []


# --- reviewer regressions: double sends, stuck jobs, fall-through -----------

def _held_email_application(conn, job, *, status="ready_to_submit"):
    draft = {"to": "jobs@acme.example.com", "subject": "Application", "body": "Dear team", "attachment_name": None}
    return db.create_application(
        conn, job_id=job["id"], channel="email:acme.example.com", status=status,
        email_to=draft["to"], email_draft=json.dumps(draft),
    )


def _record_send_draft(monkeypatch):
    calls = []
    monkeypatch.setattr(apply_module.mailer, "send_draft", lambda conn, cfg, app_id, draft, cv: calls.append(app_id))
    return calls


def test_release_email_refuses_second_draft_when_already_sent(config, monkeypatch):
    job = _seed_job_with_channels(config, cv_email="jobs@acme.example.com")
    conn = db.get_connection(config)
    try:
        _held_email_application(conn, job, status="sent")
        second_id = _held_email_application(conn, job)
    finally:
        conn.close()
    send_calls = _record_send_draft(monkeypatch)

    result = apply_module.release_application(second_id)

    assert send_calls == []
    assert result["status"] == "failed"
    assert "already sent" in result["reason"]
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, second_id)
    finally:
        conn.close()
    assert application["status"] == "discarded"
    assert "already sent" in application["notes"]


def test_release_email_refuses_when_daily_cap_reached(config, monkeypatch):
    job = _seed_job_with_channels(config, cv_email="jobs@acme.example.com")
    conn = db.get_connection(config)
    try:
        db.set_setting(conn, "email_daily_cap", 0)
        application_id = _held_email_application(conn, job)
    finally:
        conn.close()
    send_calls = _record_send_draft(monkeypatch)

    result = apply_module.release_application(application_id)

    assert send_calls == []
    assert result["status"] == "failed"
    assert "cap" in result["reason"]
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, application_id)
    finally:
        conn.close()
    assert application["status"] == "ready_to_submit"
    assert "cap" in application["notes"]


def test_plan_channels_drops_email_when_a_draft_is_already_held(config):
    job = _seed_job_with_channels(config, cv_email="jobs@acme.example.com")
    conn = db.get_connection(config)
    try:
        _held_email_application(conn, job)
        plan = apply_module.plan_channels(job, conn)
    finally:
        conn.close()
    assert [kind for kind, _ in plan] == ["job_url"]


def test_failed_email_release_does_not_duplicate_help_item(config, monkeypatch):
    job = _seed_job_with_channels(config, cv_email="jobs@acme.example.com")
    conn = db.get_connection(config)
    try:
        application_id = _held_email_application(conn, job)
    finally:
        conn.close()

    def raising_send_draft(*a, **kw):
        raise apply_module.mailer.SmtpNotConfigured("nope")

    monkeypatch.setattr(apply_module.mailer, "send_draft", raising_send_draft)

    apply_module.release_application(application_id)
    apply_module.release_application(application_id)

    conn = db.get_connection(config)
    try:
        help_items = db.list_open_help_items(conn)
    finally:
        conn.close()
    assert len(help_items) == 1


def test_content_generation_crash_never_leaves_job_applying(config, monkeypatch):
    job = _seed_job(config)

    def exploding_content(*a, **kw):
        raise RuntimeError("sonnet exploded")

    monkeypatch.setattr(answers_module, "generate_application_content", exploding_content)
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(FakePage()))

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "failed"
    assert "sonnet exploded" in result["reason"]
    conn = db.get_connection(config)
    try:
        job_row = db.get_job(conn, job["id"])
        help_items = db.list_open_help_items(conn)
    finally:
        conn.close()
    assert job_row["status"] == "needs_help"
    assert len(help_items) == 1


def test_failed_final_goto_records_the_channel_attempted(config, monkeypatch):
    job = _seed_job_with_channels(config, company_site_url="https://acme.example.com/jobs/42")

    class GotoFailsPage(FakePage):
        def goto(self, url, wait_until=None):
            self.goto_calls.append(url)
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    page = GotoFailsPage()
    monkeypatch.setattr(apply_module, "browser_page", _fake_browser_page_factory(page))
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "failed"
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, result["application_id"])
    finally:
        conn.close()
    assert application["apply_url"] == "https://boards.example.com/acme/999"
    assert application["channel"] == "generic:boards.example.com"


def test_handler_cannot_find_form_or_submit_button_falls_through():
    for reason in (
        "Could not find the Comeet application form.",
        "Could not find the Lever submit button.",
        "Could not find the Comeet submit button.",
        "Could not find the Greenhouse submit button.",
    ):
        assert apply_module._should_fall_through({"status": "needs_help", "reason": reason}, is_last=False)


# --- visible mode ------------------------------------------------------------

def _recording_browser_page(page, seen):
    @contextmanager
    def _bp(config, headless=True, visible=False):
        seen.append({"headless": headless, "visible": visible})
        yield page

    return _bp


def _set_visible(config, value=True):
    conn = db.get_connection(config)
    try:
        db.set_setting(conn, "browser_visible_mode", value)
    finally:
        conn.close()


def test_visible_mode_starts_display_and_opens_visible_browser(config, monkeypatch):
    job = _seed_job(config)
    _set_visible(config)
    seen = []
    monkeypatch.setattr(apply_module, "browser_page", _recording_browser_page(FakePage(), seen))
    monkeypatch.setattr(apply_module.display, "start_all", lambda cfg: {"all_running": True})
    handler, _ = _fake_handler({"status": "ready_to_submit", "reason": None})
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "ready_to_submit"
    assert seen == [{"headless": False, "visible": True}]


def test_visible_mode_falls_back_to_headless_when_display_fails(config, monkeypatch):
    job = _seed_job(config)
    _set_visible(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger="manual")
    finally:
        conn.close()
    seen = []
    monkeypatch.setattr(apply_module, "browser_page", _recording_browser_page(FakePage(), seen))

    def boom(cfg):
        raise apply_module.display.DisplayStackError("x11vnc is not installed")

    monkeypatch.setattr(apply_module.display, "start_all", boom)
    handler, _ = _fake_handler({"status": "ready_to_submit", "reason": None})
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False, run_id=run_id)

    assert result["status"] == "ready_to_submit"
    assert seen == [{"headless": True, "visible": False}]
    conn = db.get_connection(config)
    try:
        rows = conn.execute(
            "SELECT message FROM run_events WHERE run_id = ? AND phase = 'apply' AND kind = 'error'",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    assert any("headless" in r["message"] for r in rows)


def test_visible_mode_off_never_starts_display(config, monkeypatch):
    job = _seed_job(config)
    seen = []
    monkeypatch.setattr(apply_module, "browser_page", _recording_browser_page(FakePage(), seen))

    def never(cfg):
        raise AssertionError("display must not start when visible mode is off")

    monkeypatch.setattr(apply_module.display, "start_all", never)
    handler, _ = _fake_handler({"status": "ready_to_submit", "reason": None})
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, page: handler)
    _patch_claude(monkeypatch)

    result = apply_module.apply_to_job(job, auto_submit=False)
    assert seen == [{"headless": True, "visible": False}]

    release = apply_module.release_application(result["application_id"])
    assert release is not None
    assert seen[-1] == {"headless": True, "visible": False}

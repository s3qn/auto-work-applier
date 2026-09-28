"""Mailer tests: `smtplib.SMTP` is always a fake recording `ehlo`,
`starttls`, `login`, and `send_message`. No test opens a real socket."""

from __future__ import annotations

import smtplib
import stat
from pathlib import Path

import pytest

from autojob import db
from autojob.apply import mailer


class FakeSmtp:
    """Records the call sequence a real `smtplib.SMTP` context manager
    would receive, and lets one test make `login` raise."""

    instances: list["FakeSmtp"] = []
    login_error: Exception | None = None

    def __init__(self, host, port, timeout=30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list = []
        self.sent_message = None
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))
        if FakeSmtp.login_error is not None:
            raise FakeSmtp.login_error

    def send_message(self, msg):
        self.calls.append("send_message")
        self.sent_message = msg


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSmtp.instances = []
    FakeSmtp.login_error = None
    yield


def _smtp_values():
    return {
        "host": "smtp.gmail.com",
        "port": 587,
        "username": "sean@larch-networks.com",
        "password": "app-pass",
        "from_address": "sean@larch-networks.com",
    }


def test_draft_email_builds_subject_and_contact_block(tmp_path):
    cv_path = tmp_path / "resume.pdf"
    cv_path.write_bytes(b"%PDF-1.4 fake")
    profile_answers = {
        "full_name": "Sean Cohen",
        "email": "sean@example.com",
        "phone": "050-1234567",
        "linkedin_url": "https://linkedin.com/in/sean",
    }
    draft = mailer.draft_email(
        to="jobs@acme.com",
        job_title="Junior Penetration Tester",
        profile_answers=profile_answers,
        cover_letter="Dear Acme team,\n\nI would like to apply.",
        cv_path=cv_path,
    )
    assert draft["to"] == "jobs@acme.com"
    assert draft["subject"] == "Application for Junior Penetration Tester (Sean Cohen)"
    assert "Email: sean@example.com" in draft["body"]
    assert "Phone: 050-1234567" in draft["body"]
    assert "LinkedIn: https://linkedin.com/in/sean" in draft["body"]
    assert draft["attachment_name"] == "resume.pdf"


def test_draft_email_with_no_cv_has_no_attachment_name():
    draft = mailer.draft_email(
        to="jobs@acme.com",
        job_title="Junior Penetration Tester",
        profile_answers={},
        cover_letter="Dear Acme team,",
        cv_path=None,
    )
    assert draft["attachment_name"] is None


def test_build_message_attaches_cv_with_its_filename(tmp_path):
    cv_path = tmp_path / "resume.pdf"
    cv_path.write_bytes(b"%PDF-1.4 fake")
    draft = {"to": "jobs@acme.com", "subject": "Subject", "body": "Body text", "attachment_name": "resume.pdf"}

    msg = mailer.build_message(draft, from_address="sean@larch-networks.com", cv_path=cv_path)

    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "resume.pdf"
    assert attachments[0].get_content_type() == "application/pdf"


def test_build_message_without_cv_has_no_attachment():
    draft = {"to": "jobs@acme.com", "subject": "Subject", "body": "Body text", "attachment_name": None}

    msg = mailer.build_message(draft, from_address="sean@larch-networks.com", cv_path=None)

    assert list(msg.iter_attachments()) == []


def test_render_preview_shows_attachment_name(tmp_path):
    cv_path = tmp_path / "resume.pdf"
    cv_path.write_bytes(b"%PDF-1.4 fake")
    draft = {"to": "jobs@acme.com", "subject": "Subject", "body": "Body text", "attachment_name": "resume.pdf"}
    msg = mailer.build_message(draft, from_address="sean@larch-networks.com", cv_path=cv_path)

    preview = mailer.render_preview(msg)

    assert "Attachment: resume.pdf" in preview
    assert "Body text" in preview


def test_save_smtp_config_writes_mode_0600(config):
    path = mailer.save_smtp_config(config, _smtp_values())

    assert path == config.smtp_path
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    loaded = mailer.load_smtp_config(config)
    assert loaded["username"] == "sean@larch-networks.com"


def test_load_smtp_config_returns_none_when_missing(config):
    assert mailer.load_smtp_config(config) is None


def test_send_draft_raises_smtp_not_configured_when_missing(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        draft = {"to": "jobs@acme.com", "subject": "Subject", "body": "Body", "attachment_name": None}
        with pytest.raises(mailer.SmtpNotConfigured):
            mailer.send_draft(conn, config, 1, draft, None)
    finally:
        conn.close()


def test_send_message_wraps_auth_failure_as_email_send_error(monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSmtp)
    FakeSmtp.login_error = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    msg = mailer.build_message(
        {"to": "a@b.com", "subject": "s", "body": "b", "attachment_name": None},
        from_address="sean@larch-networks.com",
        cv_path=None,
    )

    with pytest.raises(mailer.EmailSendError):
        mailer.send_message(msg, _smtp_values())


def test_send_draft_sends_and_saves_two_proof_rows(config, monkeypatch):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        job_id = db.create_job(
            conn, source="manual", url="https://example.com/job", title="Pentester", company_id=company_id
        )
        application_id = db.create_application(conn, job_id=job_id, channel="email", status="ready_to_submit")

        mailer.save_smtp_config(config, _smtp_values())
        monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSmtp)

        cv_path = config.data_dir / "cv.pdf"
        cv_path.write_bytes(b"%PDF-1.4 fake")
        draft = {
            "to": "jobs@acme.com",
            "subject": "Application for Pentester (Sean)",
            "body": "Body text",
            "attachment_name": "cv.pdf",
        }

        mailer.send_draft(conn, config, application_id, draft, cv_path)

        fake = FakeSmtp.instances[-1]
        assert fake.calls == [
            "ehlo",
            "starttls",
            "ehlo",
            ("login", "sean@larch-networks.com", "app-pass"),
            "send_message",
        ]

        rows = db.list_proofs(conn, application_id)
        sent_rows = [r for r in rows if r["kind"] == "sent_email"]
        assert len(sent_rows) == 2
        names = sorted(Path(r["path"]).name for r in sent_rows)
        assert names == ["sent.eml", "sent_email.txt"]
        for row in sent_rows:
            assert Path(row["path"]).exists()
    finally:
        conn.close()


def test_send_draft_records_the_send_even_when_proof_saving_fails(config, monkeypatch):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        job_id = db.create_job(
            conn, source="manual", url="https://example.com/job", title="Pentester", company_id=company_id
        )
        application_id = db.create_application(
            conn, job_id=job_id, channel="email:acme.com", status="ready_to_submit", email_to="jobs@acme.com",
        )
        mailer.save_smtp_config(config, _smtp_values())
        monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSmtp)

        def broken_proofs(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(mailer, "save_sent_proofs", broken_proofs)
        draft = {"to": "jobs@acme.com", "subject": "Application", "body": "Body", "attachment_name": None}

        mailer.send_draft(conn, config, application_id, draft, None)

        application = db.get_application(conn, application_id)
        assert application["status"] == "sent"
        assert "disk full" in application["notes"]
        assert db.email_already_sent(conn, job_id, "jobs@acme.com")
    finally:
        conn.close()


def test_save_smtp_config_creates_file_0600_from_the_start(config, monkeypatch):
    opened = []
    real_open = mailer.os.open

    def spy_open(path, flags, mode=0o777, *a, **kw):
        opened.append((str(path), mode))
        return real_open(path, flags, mode, *a, **kw)

    monkeypatch.setattr(mailer.os, "open", spy_open)
    mailer.save_smtp_config(config, _smtp_values())
    assert opened == [(str(config.smtp_path), 0o600)]


def test_save_smtp_config_tightens_a_pre_existing_file(config):
    config.ensure_dirs()
    config.smtp_path.write_text("{}")
    config.smtp_path.chmod(0o644)
    mailer.save_smtp_config(config, _smtp_values())
    assert stat.S_IMODE(config.smtp_path.stat().st_mode) == 0o600
    assert mailer.load_smtp_config(config)["password"] == "app-pass"

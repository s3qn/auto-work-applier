"""Proof capture tests: every screenshot/PDF call is a fake Playwright page
that just writes a marker file, no real browser."""

from __future__ import annotations

from pathlib import Path

from autojob import db
from autojob.apply import proof


class FakePage:
    def __init__(self, html="<html>ok</html>", pdf_raises=False):
        self.html = html
        self.pdf_raises = pdf_raises
        self.screenshot_paths: list[str] = []

    def screenshot(self, path, full_page=False):
        assert full_page is True
        Path(path).write_bytes(b"fake png")
        self.screenshot_paths.append(path)

    def content(self):
        return self.html

    def pdf(self, path):
        if self.pdf_raises:
            raise RuntimeError("PDF export is not available for this page.")
        Path(path).write_bytes(b"%PDF-1.4 fake")


def _conn(config):
    db.init_db(config)
    return db.get_connection(config)


def _seed_application(conn):
    company_id = db.get_or_create_company(conn, "Acme")
    job_id = db.create_job(
        conn, source="manual", url="https://example.com/job", title="Pentester", company_id=company_id
    )
    return db.create_application(conn, job_id=job_id, channel="greenhouse", status="ready_to_submit")


def test_capture_form_screenshot_writes_file_and_proof_row(config):
    conn = _conn(config)
    try:
        application_id = _seed_application(conn)
        page = FakePage()
        path = proof.capture_form_screenshot(conn, config, application_id, page)

        assert path.exists()
        rows = db.list_proofs(conn, application_id)
        assert len(rows) == 1
        assert rows[0]["kind"] == "screenshot"
        assert rows[0]["path"] == str(path)
    finally:
        conn.close()


def test_capture_confirmation_writes_screenshot_html_and_pdf_rows(config):
    conn = _conn(config)
    try:
        application_id = _seed_application(conn)
        page = FakePage(html="<html>Thanks for applying</html>")
        proof.capture_confirmation(conn, config, application_id, page)

        rows = db.list_proofs(conn, application_id)
        kinds = sorted(r["kind"] for r in rows)
        assert kinds == ["confirmation", "confirmation", "pdf"]

        html_row = next(r for r in rows if r["path"].endswith(".html"))
        assert "Thanks for applying" in Path(html_row["path"]).read_text()

        pdf_row = next(r for r in rows if r["path"].endswith(".pdf"))
        assert Path(pdf_row["path"]).exists()
    finally:
        conn.close()


def test_capture_confirmation_pdf_failure_is_non_fatal(config):
    conn = _conn(config)
    try:
        application_id = _seed_application(conn)
        page = FakePage(pdf_raises=True)
        proof.capture_confirmation(conn, config, application_id, page)

        rows = db.list_proofs(conn, application_id)
        kinds = sorted(r["kind"] for r in rows)
        # No pdf row when the export fails, but the screenshot and html
        # still land: one bad proof step never loses the others.
        assert kinds == ["confirmation", "confirmation"]
    finally:
        conn.close()


def test_save_cover_letter_writes_the_exact_text(config):
    application_id = 42
    path = proof.save_cover_letter(config, application_id, "Dear hiring team,\n\nHonest letter.")
    assert path.read_text() == "Dear hiring team,\n\nHonest letter."
    assert str(application_id) in str(path)


def test_application_dir_is_under_data_applications(config):
    path = proof.application_dir(config, 7)
    assert path == config.data_dir / "applications" / "7"
    assert path.is_dir()

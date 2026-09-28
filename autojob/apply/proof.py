"""Proof capture for an application.

Every application gets a full-page screenshot of the filled form before any
submit click, whether or not `auto_submit` is on. A real submit additionally
captures the confirmation page: a screenshot, its raw HTML, and a PDF if the
browser can produce one (best-effort; not every confirmation page renders a
usable PDF, especially one reached inside an ATS's own iframe, and that
never blocks the rest of the apply flow). Everything lands under
`data/applications/<application_id>/` with one `proofs` row per file.
"""

from __future__ import annotations

from pathlib import Path

from autojob import db
from autojob.config import Config


def application_dir(config: Config, application_id: int) -> Path:
    path = config.data_dir / "applications" / str(application_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def capture_form_screenshot(conn, config: Config, application_id: int, page) -> Path:
    path = application_dir(config, application_id) / "form.png"
    page.screenshot(path=str(path), full_page=True)
    db.create_proof(conn, application_id=application_id, kind="screenshot", path=str(path))
    return path


def capture_confirmation(conn, config: Config, application_id: int, page) -> None:
    out_dir = application_dir(config, application_id)

    screenshot_path = out_dir / "confirmation.png"
    page.screenshot(path=str(screenshot_path), full_page=True)
    db.create_proof(conn, application_id=application_id, kind="confirmation", path=str(screenshot_path))

    html_path = out_dir / "confirmation.html"
    html_path.write_text(page.content(), encoding="utf-8")
    db.create_proof(conn, application_id=application_id, kind="confirmation", path=str(html_path))

    try:
        pdf_path = out_dir / "confirmation.pdf"
        page.pdf(path=str(pdf_path))
        db.create_proof(conn, application_id=application_id, kind="pdf", path=str(pdf_path))
    except Exception:  # noqa: BLE001 - PDF export isn't always available; never fatal
        pass


def save_cover_letter(config: Config, application_id: int, cover_letter: str) -> Path:
    """Save the exact cover letter text used, alongside the other proof
    files. Also the file a Comeet-style file-only cover letter field
    uploads, see `autojob.apply.handlers.comeet`."""
    path = application_dir(config, application_id) / "cover_letter.txt"
    path.write_text(cover_letter, encoding="utf-8")
    return path

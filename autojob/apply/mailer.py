"""Email apply channel: draft, send and prove a job application email.

There is no second LLM call here. The body reuses the cover letter already
generated for the job (`answers.generate_application_content`, the same
text Sean reviews on the application detail page) with a short contact
block appended from `profile.answers`. Credentials live in `data/smtp.json`
(0600, written by `uv run python -m autojob.set_smtp`), never in the
database or in code: `smtp.gmail.com:587` with STARTTLS and a Google App
Password for the default sender.

`send_draft` is the single entry point the apply orchestrator and
`release_application` call: it loads the saved credentials, sends the
message, and saves proof of exactly what went out. A missing config raises
`SmtpNotConfigured`; a connection, auth, or protocol failure raises
`EmailSendError`. Neither is caught here: the caller decides what a failed
send means for the application and the job (see `autojob/apply/apply.py`).
Once the message is out, `send_draft` marks the application `sent` itself,
before saving proof, so a proof failure (noted on the application) never
leaves a delivered email looking unsent.
"""

from __future__ import annotations

import email.utils
import json
import os
import smtplib
import ssl
import stat
from email.message import EmailMessage
from pathlib import Path

from autojob import db
from autojob.apply import proof
from autojob.config import Config

SMTP_DEFAULTS = {
    "host": "smtp.gmail.com",
    "port": 587,
    "username": "sean@larch-networks.com",
}


class SmtpNotConfigured(Exception):
    """No `data/smtp.json` on disk yet."""


class EmailSendError(Exception):
    """The connection, STARTTLS handshake, login, or send itself failed."""


def load_smtp_config(config: Config) -> dict | None:
    if not config.smtp_path.exists():
        return None
    return json.loads(config.smtp_path.read_text())


def save_smtp_config(config: Config, values: dict) -> Path:
    config.ensure_dirs()
    # Created 0600 from the first byte, never world-readable in between. The
    # mode argument only applies to a new file, so a pre-existing one is
    # tightened with chmod as well.
    fd = os.open(config.smtp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(values))
    os.chmod(config.smtp_path, stat.S_IRUSR | stat.S_IWUSR)
    return config.smtp_path


def build_subject(job_title: str, full_name: str) -> str:
    return f"Application for {job_title} ({full_name})"


def draft_email(*, to: str, job_title: str, profile_answers: dict, cover_letter: str, cv_path) -> dict:
    """Build the {"to", "subject", "body", "attachment_name"} dict stored on
    the application row. `cv_path` is only used for its filename here; the
    file itself is attached later, in `build_message`."""
    full_name = profile_answers.get("full_name") or "Sean"
    contact_lines = []
    for key, label in (("email", "Email"), ("phone", "Phone"), ("linkedin_url", "LinkedIn")):
        value = (profile_answers.get(key) or "").strip()
        if value:
            contact_lines.append(f"{label}: {value}")
    body = cover_letter.strip()
    if contact_lines:
        body = f"{body}\n\n" + "\n".join(contact_lines)
    return {
        "to": to,
        "subject": build_subject(job_title, full_name),
        "body": body,
        "attachment_name": Path(cv_path).name if cv_path else None,
    }


def build_message(draft: dict, *, from_address: str, cv_path) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = draft["to"]
    msg["Subject"] = draft["subject"]
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    msg.set_content(draft["body"])
    if cv_path:
        cv_file = Path(cv_path)
        msg.add_attachment(
            cv_file.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=cv_file.name,
        )
    return msg


def send_message(msg: EmailMessage, smtp_config: dict) -> None:
    try:
        with smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(smtp_config["username"], smtp_config["password"])
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailSendError(str(exc)) from exc


def render_preview(msg: EmailMessage) -> str:
    """The From/To/Subject/Date/Attachment header block plus body saved as
    `sent_email.txt`, a human-readable companion to the raw `sent.eml`."""
    lines = [
        f"From: {msg['From']}",
        f"To: {msg['To']}",
        f"Subject: {msg['Subject']}",
        f"Date: {msg['Date']}",
    ]
    attachment_name = None
    for part in msg.iter_attachments():
        attachment_name = part.get_filename()
        break
    if attachment_name:
        lines.append(f"Attachment: {attachment_name}")
    body_part = msg.get_body(preferencelist=("plain",))
    body = body_part.get_content() if body_part is not None else ""
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def save_sent_proofs(conn, config: Config, application_id: int, msg: EmailMessage) -> None:
    out_dir = proof.application_dir(config, application_id)

    eml_path = out_dir / "sent.eml"
    eml_path.write_bytes(msg.as_bytes())
    db.create_proof(conn, application_id=application_id, kind="sent_email", path=str(eml_path))

    txt_path = out_dir / "sent_email.txt"
    txt_path.write_text(render_preview(msg), encoding="utf-8")
    db.create_proof(conn, application_id=application_id, kind="sent_email", path=str(txt_path))


def send_draft(conn, config: Config, application_id: int, draft: dict, cv_path) -> None:
    smtp_config = load_smtp_config(config)
    if smtp_config is None:
        raise SmtpNotConfigured("SMTP not configured. Run: uv run python -m autojob.set_smtp")
    from_address = smtp_config.get("from_address") or smtp_config["username"]
    msg = build_message(draft, from_address=from_address, cv_path=cv_path)
    send_message(msg, smtp_config)
    # The email is out: record it as sent before anything else can fail, so
    # a proof-saving error can never make a later run send it again.
    db.update_application(conn, application_id, status="sent")
    try:
        save_sent_proofs(conn, config, application_id, msg)
    except Exception as exc:  # noqa: BLE001 - the send already happened, only the proof is missing
        db.update_application(conn, application_id, notes=f"Email sent, but saving its proof failed: {exc}")

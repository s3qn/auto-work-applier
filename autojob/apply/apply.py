"""Top-level apply entry points.

`apply_to_job(job, *, auto_submit)` opens a browser page, picks a handler by
the job's application URL, fills the form with an honest answer set,
captures proof, and records the result as an application row. When
`auto_submit` is false (the default) it stops before the final submit click
and leaves the application as `ready_to_submit`; `release_application` is
what a later "Release" click from the dashboard uses to go back and
actually submit that same, already-filled application.

Company-site crawling to find an application link is intentionally not
built yet: `_resolve_apply_url` uses `companies.careers_url` from research
when present, otherwise the job's own URL, exactly as scoped. Anything that
isn't Greenhouse, Lever, or Comeet becomes a `needs_help` item rather than a
guess.
"""

from __future__ import annotations

import json

from autojob import db
from autojob.apply import answers as answers_module
from autojob.apply import proof
from autojob.apply.router import pick_handler
from autojob.browser.context import BrowserBusyError, browser_page
from autojob.config import load_config
from autojob.llm.cv import latest_cv_pdf
from autojob.web.deps import get_answer_bank, get_profile_answers

CHANNEL_BY_HANDLER_NAME = {"greenhouse": "greenhouse", "lever": "lever", "comeet": "comeet"}


def _row_get(row, key: str, default=None):
    """Read `key` from a sqlite3.Row or a plain dict, tolerating rows that
    don't carry every optional column (test fixtures, mostly)."""
    try:
        keys = row.keys()
    except AttributeError:
        keys = row
    return row[key] if key in keys else default


def _resolve_apply_url(job) -> str:
    return _row_get(job, "company_careers_url") or job["url"]


def _job_facts(job) -> dict:
    return {
        "title": job["title"],
        "company": _row_get(job, "company_name", ""),
        "description": _row_get(job, "description", ""),
    }


def _handler_channel(handler) -> str:
    name = handler.__name__.rsplit(".", 1)[-1]
    return CHANNEL_BY_HANDLER_NAME.get(name, "other")


def apply_to_job(job, *, auto_submit: bool) -> dict:
    """Apply to one job. `job` is a row from `db.get_job_with_company` (or
    anything with the same keys: id, url, title, description, company_name,
    company_tldr, company_careers_url).

    Returns {"status", "application_id", "reason"}. Never raises: a browser
    problem, an unrecognized ATS, or a field the handler can't honestly
    answer all resolve to a recorded outcome (a `failed` application or a
    `needs_help` item) instead of crashing the caller, so one bad job never
    stops a scan or a background "Apply now" click.
    """
    config = load_config()
    conn = db.get_connection(config)
    try:
        job_id = job["id"]
        db.update_job_status(conn, job_id, "applying")
        apply_url = _resolve_apply_url(job)
        try:
            with browser_page(config, headless=True) as page:
                page.goto(apply_url, wait_until="domcontentloaded")
                return _fill_and_record(conn, config, job, job_id, apply_url, page, auto_submit)
        except BrowserBusyError as exc:
            db.update_job_status(conn, job_id, "queued")
            return {"status": "failed", "application_id": None, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a browser-level failure must never crash the caller
            reason = f"Apply attempt failed before a form could be filled: {exc}"
            db.create_help_item(conn, job_id=job_id, reason=reason)
            db.update_job_status(conn, job_id, "needs_help")
            return {"status": "failed", "application_id": None, "reason": reason}
    finally:
        conn.close()


def _fill_and_record(conn, config, job, job_id, apply_url, page, auto_submit: bool) -> dict:
    handler = pick_handler(apply_url, page)
    if handler is None:
        reason = f"No apply handler recognizes this ATS yet ({apply_url})."
        db.create_help_item(conn, job_id=job_id, reason=reason)
        db.update_job_status(conn, job_id, "needs_help")
        return {"status": "needs_help", "application_id": None, "reason": reason}

    profile_answers = get_profile_answers(conn)
    answer_bank = get_answer_bank(conn)
    merged_answers = answers_module.build_answer_set(profile_answers, answer_bank)

    company_tldr = _row_get(job, "company_tldr", "")
    work_dir = config.data_dir / "llm-work"
    content = answers_module.generate_application_content(
        profile_answers, answer_bank, _job_facts(job), company_tldr, work_dir=work_dir
    )
    merged_answers["cover_letter"] = content["cover_letter"]
    merged_answers.update(content["screening_answers"])

    cv_path = latest_cv_pdf(config)
    application_id = db.create_application(
        conn,
        job_id=job_id,
        channel=_handler_channel(handler),
        status="needs_help",  # placeholder, corrected below once the handler actually runs
        cover_letter=content["cover_letter"],
        answers_json=json.dumps(content["screening_answers"]),
        cv_path=str(cv_path) if cv_path else None,
        apply_url=apply_url,
    )
    cover_letter_path = proof.save_cover_letter(config, application_id, content["cover_letter"])
    merged_answers["cover_letter_file"] = str(cover_letter_path)

    return _run_handler_and_finalize(conn, config, job_id, application_id, handler, page, merged_answers, cv_path, auto_submit)


def _run_handler_and_finalize(conn, config, job_id, application_id, handler, page, merged_answers, cv_path, auto_submit: bool) -> dict:
    try:
        result = handler.apply(page, merged_answers, cv_path, auto_submit)
    except Exception as exc:  # noqa: BLE001 - a handler bug must never crash the run
        reason = f"Apply handler raised an error: {exc}"
        db.update_application(conn, application_id, status="failed", notes=reason)
        db.update_job_status(conn, job_id, "failed")
        return {"status": "failed", "application_id": application_id, "reason": reason}

    proof.capture_form_screenshot(conn, config, application_id, page)

    status = result.get("status", "needs_help")
    reason = result.get("reason")
    if status == "sent":
        proof.capture_confirmation(conn, config, application_id, page)
        db.update_application(conn, application_id, status="sent")
        db.update_job_status(conn, job_id, "applied")
    elif status == "ready_to_submit":
        db.update_application(conn, application_id, status="ready_to_submit")
        db.update_job_status(conn, job_id, "applying")
    elif status == "needs_help":
        db.update_application(conn, application_id, status="needs_help", notes=reason)
        db.create_help_item(conn, job_id=job_id, reason=reason or "Apply handler needs input.")
        db.update_job_status(conn, job_id, "needs_help")
    else:
        db.update_application(conn, application_id, status="failed", notes=reason)
        db.update_job_status(conn, job_id, "failed")

    return {"status": status, "application_id": application_id, "reason": reason}


def release_application(application_id: int) -> dict:
    """Sean releasing a `ready_to_submit` application from the dashboard:
    go back to the same apply URL, refill it with the answers already saved
    on the application (no new LLM call, so the cover letter stays exactly
    what he reviewed), and this time actually submit."""
    config = load_config()
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, application_id)
        if application is None:
            return {"status": "failed", "reason": "Application not found."}
        job = db.get_job_with_company(conn, application["job_id"])
        if job is None:
            return {"status": "failed", "reason": "Job not found."}

        apply_url = application["apply_url"] or _resolve_apply_url(job)
        try:
            with browser_page(config, headless=True) as page:
                page.goto(apply_url, wait_until="domcontentloaded")
                handler = pick_handler(apply_url, page)
                if handler is None:
                    reason = f"No apply handler recognizes this ATS anymore ({apply_url})."
                    db.update_application(conn, application_id, status="failed", notes=reason)
                    return {"status": "failed", "reason": reason}

                profile_answers = get_profile_answers(conn)
                answer_bank = get_answer_bank(conn)
                merged_answers = answers_module.build_answer_set(profile_answers, answer_bank)
                merged_answers["cover_letter"] = application["cover_letter"] or ""
                if application["answers_json"]:
                    merged_answers.update(json.loads(application["answers_json"]))
                merged_answers["cover_letter_file"] = str(
                    proof.save_cover_letter(config, application_id, merged_answers["cover_letter"])
                )
                cv_path = application["cv_path"]

                result = handler.apply(page, merged_answers, cv_path, auto_submit=True)
                proof.capture_form_screenshot(conn, config, application_id, page)

                status = result.get("status", "needs_help")
                reason = result.get("reason")
                if status == "sent":
                    proof.capture_confirmation(conn, config, application_id, page)
                    db.update_application(conn, application_id, status="sent")
                    db.update_job_status(conn, job["id"], "applied")
                elif status == "needs_help":
                    db.update_application(conn, application_id, status="needs_help", notes=reason)
                    db.create_help_item(conn, job_id=job["id"], reason=reason or "Release needs input.")
                    db.update_job_status(conn, job["id"], "needs_help")
                else:
                    db.update_application(conn, application_id, status="failed", notes=reason)
                return {"status": status, "reason": reason}
        except BrowserBusyError as exc:
            return {"status": "failed", "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - releasing must never crash the caller
            reason = f"Release failed: {exc}"
            db.update_application(conn, application_id, status="failed", notes=reason)
            return {"status": "failed", "reason": reason}
    finally:
        conn.close()

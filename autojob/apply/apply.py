"""Top-level apply entry points.

`apply_to_job(job, *, auto_submit)` opens a browser page once and walks a
channel chain built by `plan_channels`: the company's own posting
(`jobs.company_site_url`), a specific careers-page posting, a general "send
us your CV" form, a published jobs/HR email, and finally the board URL the
job was found on (a fresh account with no research still sees only that
last channel, today's behaviour). Greenhouse, Lever and Comeet get their
deterministic handlers on any page-based channel; every other ATS (a
company career page, AllJobs, LinkedIn) goes through `handlers/generic.py`,
the browser-plus-LLM fallback, rather than escalating to `needs_help`
without an attempt.

A `needs_help` or `failed` result on a non-final channel falls through to
the next one only when the reason looks like the channel itself was
unusable (a CAPTCHA, no form found, a signup wall, a crash): see
`FALL_THROUGH_MARKERS`. A required question the handler can't honestly
answer, or a login requirement, escalates immediately instead: the form is
usable and only Sean's answer is missing, so trying a lesser channel would
defeat the point. The email channel is always terminal, whatever its
outcome: it never falls through to the board URL.

`release_application` is what a later "Release" click from the dashboard
uses to go back and actually submit an already-filled `ready_to_submit`
application: `mailer.send_draft` for a held email (no browser, no new LLM
call), otherwise refilling and submitting the same apply URL.

`requeue_stale_no_handler_jobs` is a one-time maintenance function: before
the generic handler existed, any job on an unrecognized ATS was escalated
without being attempted, tagged with the reason
"No apply handler recognizes this ATS...". It resets those jobs to `queued`
and deletes the now-stale help items, so the next apply pass gives them the
attempt they never got.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlparse

from autojob import db
from autojob.apply import answers as answers_module
from autojob.apply import mailer
from autojob.apply import proof
from autojob.apply.router import pick_handler, pick_handler_by_url
from autojob.browser import display
from autojob.browser.context import BrowserBusyError, browser_page
from autojob.config import load_config
from autojob.llm.cv import latest_cv_pdf
from autojob.pipeline import events
from autojob.web.deps import (
    JERUSALEM,
    get_answer_bank,
    get_browser_visible_mode,
    get_email_daily_cap,
    get_profile_answers,
)

CHANNEL_BY_HANDLER_NAME = {"greenhouse": "greenhouse", "lever": "lever", "comeet": "comeet"}
STALE_NO_HANDLER_REASON_MARKER = "No apply handler recognizes this ATS"

# A needs_help/failed reason containing one of these looks like the channel
# itself was unusable, so the chain tries the next one instead of stopping.
# Anything else (an unanswerable required question, a login wall) escalates
# immediately: skipping the company's own form for a lesser channel would
# defeat the point of trying it first.
FALL_THROUGH_MARKERS = (
    "CAPTCHA",
    "Could not locate",
    "Could not find a submit control",
    "Could not find the ",  # "... the Comeet application form.", "... the Lever submit button."
    "Could not open",
    "Account signup required",
    "unexpected error",
    "Apply handler raised",
    "before a form could be filled",
)


def _row_get(row, key: str, default=None):
    """Read `key` from a sqlite3.Row or a plain dict, tolerating rows that
    don't carry every optional column (test fixtures, mostly)."""
    try:
        keys = row.keys()
    except AttributeError:
        keys = row
    return row[key] if key in keys else default


def _job_facts(job) -> dict:
    return {
        "title": job["title"],
        "company": _row_get(job, "company_name", ""),
        "description": _row_get(job, "description", ""),
    }


def _generic_channel(url: str) -> str:
    domain = urlparse(url or "").netloc.lower() or "unknown"
    return f"generic:{domain}"


def _handler_channel(handler, url: str = "") -> str:
    name = handler.__name__.rsplit(".", 1)[-1]
    if name == "generic":
        return _generic_channel(url)
    return CHANNEL_BY_HANDLER_NAME.get(name, "other")


def _job_label(job) -> tuple[str, str]:
    return job["title"], _row_get(job, "company_name", "") or "Unknown"


def _local_midnight_iso() -> str:
    """Local (Asia/Jerusalem) midnight, expressed the same way
    `db.now_iso()` stores timestamps (UTC, `%Y-%m-%dT%H:%M:%SZ`), so it can
    be compared directly against `proofs.created_at`."""
    local_midnight = datetime.now(JERUSALEM).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _careers_url_looks_like_a_posting(url: str) -> bool:
    """A bare `/careers` listing isn't a specific job to apply to; skip it
    and fall to the next channel instead of trying to fill a list page."""
    if pick_handler_by_url(url) is not None:
        return True
    segments = [s for s in urlparse(url).path.split("/") if s]
    if len(segments) < 2:
        return False
    last = segments[-1]
    return any(ch.isdigit() for ch in last) or len(last) >= 12


def plan_channels(job, conn) -> list[tuple[str, str]]:
    """The channel order Sean wants: the company's own posting first, then a
    specific careers-page posting, then a general "send us your CV" form,
    then a published jobs/HR email, and finally the board URL the job was
    found on. Duplicate targets are dropped, keeping the first occurrence."""
    candidates: list[tuple[str, str]] = []

    company_site_url = _row_get(job, "company_site_url")
    if company_site_url:
        candidates.append(("company_job", company_site_url))

    careers_url = _row_get(job, "company_careers_url")
    if careers_url and _careers_url_looks_like_a_posting(careers_url):
        candidates.append(("careers", careers_url))

    general_cv_url = _row_get(job, "company_general_cv_url")
    if general_cv_url:
        candidates.append(("general_cv", general_cv_url))

    cv_email = _row_get(job, "company_cv_email")
    if cv_email:
        cap = get_email_daily_cap(conn)
        sent_today = db.count_emails_sent_since(conn, _local_midnight_iso())
        already_sent = db.email_already_sent(conn, job["id"], cv_email)
        draft_held = db.email_draft_held_for_job(conn, job["id"])
        if not already_sent and not draft_held and sent_today < cap:
            candidates.append(("email", cv_email))

    candidates.append(("job_url", job["url"]))

    seen: set[str] = set()
    plan: list[tuple[str, str]] = []
    for kind, target in candidates:
        if target in seen:
            continue
        seen.add(target)
        plan.append((kind, target))
    return plan


def _should_fall_through(result: dict, is_last: bool) -> bool:
    if is_last:
        return False
    if result.get("status") not in ("needs_help", "failed"):
        return False
    reason = result.get("reason") or ""
    return any(marker in reason for marker in FALL_THROUGH_MARKERS)


def _visible_mode(conn, config, run_id) -> bool:
    """True when the apply should run on the watchable display. Starts the
    display stack itself; if it cannot start, falls back to headless (and
    says so in the run log) instead of failing the apply."""
    if not get_browser_visible_mode(conn):
        return False
    try:
        display.start_all(config)
    except Exception as exc:  # noqa: BLE001 - any display failure falls back to headless
        if run_id is not None:
            events.emit(
                conn, run_id, "apply", "error",
                f"Browser view unavailable ({exc}); running headless",
            )
        return False
    return True


def apply_to_job(job, *, auto_submit: bool, run_id: int | None = None) -> dict:
    """Apply to one job. `job` is a row from `db.get_job_with_company` (or
    anything with the same keys). Returns {"status", "application_id",
    "reason"}. Never raises: a browser problem, an unrecognized ATS, or a
    field the handler can't honestly answer all resolve to a recorded
    outcome (a `failed` application or a `needs_help` item) instead of
    crashing the caller, so one bad job never stops a scan or a background
    "Apply now" click.

    `run_id`, when given, emits `run_events` rows for the live view. Left
    `None` for calls outside a pipeline run (the "Apply now" button,
    `release_application`).
    """
    config = load_config()
    conn = db.get_connection(config)
    try:
        job_id = job["id"]
        title, company = _job_label(job)
        db.update_job_status(conn, job_id, "applying")
        application_id = None
        try:
            # Everything from here on sits inside the try, including the
            # sonnet content call before the browser lock: any error must
            # land the job in a terminal status, never leave it `applying`.
            profile_answers = get_profile_answers(conn)
            answer_bank = get_answer_bank(conn)
            merged_answers = answers_module.build_answer_set(profile_answers, answer_bank)

            company_tldr = _row_get(job, "company_tldr", "")
            job_facts = _job_facts(job)
            work_dir = config.data_dir / "llm-work"
            content = answers_module.generate_application_content(
                profile_answers, answer_bank, job_facts, company_tldr, work_dir=work_dir
            )
            merged_answers["cover_letter"] = content["cover_letter"]
            merged_answers.update(content["screening_answers"])
            # Job context for the generic handler's own LLM call (field mapping):
            # the ATS-specific handlers simply ignore these extra keys.
            merged_answers["_job_title"] = job_facts["title"]
            merged_answers["_company_name"] = job_facts["company"]
            merged_answers["_company_tldr"] = company_tldr or ""

            cv_path = latest_cv_pdf(config)
            plan = plan_channels(job, conn)
            visible_mode = _visible_mode(conn, config, run_id)
            with browser_page(config, headless=not visible_mode, visible=visible_mode) as page:
                first_kind, first_target = plan[0]
                placeholder_channel = (
                    f"email:{first_target.split('@', 1)[-1].lower()}" if first_kind == "email" else "other"
                )
                application_id = db.create_application(
                    conn,
                    job_id=job_id,
                    channel=placeholder_channel,
                    status="needs_help",  # placeholder, corrected below once the chain actually runs
                    cover_letter=content["cover_letter"],
                    answers_json=json.dumps(content["screening_answers"]),
                    cv_path=str(cv_path) if cv_path else None,
                    apply_url=first_target,
                    run_id=run_id,
                )
                cover_letter_path = proof.save_cover_letter(config, application_id, content["cover_letter"])
                merged_answers["cover_letter_file"] = str(cover_letter_path)

                return _run_channel_chain(
                    conn, config, job_id, application_id, page, plan, merged_answers,
                    content["cover_letter"], job_facts, cv_path, auto_submit,
                    run_id=run_id, title=title, company=company,
                )
        except BrowserBusyError as exc:
            db.update_job_status(conn, job_id, "queued")
            if run_id is not None:
                events.emit(conn, run_id, "apply", "error", f"apply: {title} @ {company}: {exc}")
            return {"status": "failed", "application_id": application_id, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a content, browser or planning failure must never crash the caller
            reason = f"Apply attempt failed before a form could be filled: {exc}"
            if application_id is not None:
                db.update_application(conn, application_id, status="failed", notes=reason)
            db.create_help_item(conn, job_id=job_id, reason=reason)
            db.update_job_status(conn, job_id, "needs_help")
            if run_id is not None:
                events.emit(conn, run_id, "apply", "error", f"apply: {title} @ {company}: {reason}")
            return {"status": "failed", "application_id": application_id, "reason": reason}
    finally:
        conn.close()


def _run_channel_chain(
    conn, config, job_id, application_id, page, plan, merged_answers, cover_letter, job_facts, cv_path,
    auto_submit: bool, *, run_id: int | None, title: str, company: str,
) -> dict:
    notes_lines: list[str] = []
    last_index = len(plan) - 1

    for index, (kind, target) in enumerate(plan):
        is_last = index == last_index
        if run_id is not None:
            events.emit(conn, run_id, "apply", "info", f"apply: {title} @ {company}: trying {kind} {target}.")

        if kind == "email":
            outcome = _attempt_email_channel(
                conn, config, job_id, application_id, target, job_facts, cover_letter, cv_path, auto_submit
            )
            if run_id is not None:
                events.emit(
                    conn, run_id, "apply", "info",
                    f"apply: {title} @ {company} via {outcome['channel_label']}: {outcome['status']}.",
                )
            return {"status": outcome["status"], "application_id": application_id, "reason": outcome.get("reason")}

        attempt = _attempt_page_channel(conn, application_id, page, target, merged_answers, cv_path, auto_submit)
        channel_label = attempt.get("channel_label")
        if channel_label and run_id is not None:
            events.emit(conn, run_id, "apply", "info", f"apply: {title} @ {company} via {channel_label}: filling.")

        fall_through = _should_fall_through(attempt, is_last)
        if channel_label:
            screenshot_name = f"attempt-{index + 1}-{kind}.png" if fall_through else "form.png"
            proof.capture_form_screenshot(conn, config, application_id, page, filename=screenshot_name)

        if fall_through:
            notes_lines.append(f"Tried {kind} {target}: {attempt.get('reason')}")
            db.update_application(conn, application_id, notes="\n".join(notes_lines))
            if run_id is not None:
                events.emit(
                    conn, run_id, "apply", "info",
                    f"apply: {title} @ {company} via {channel_label or kind}: {attempt['status']} (falling through).",
                )
            continue

        outcome = _finalize_page_attempt(conn, config, job_id, application_id, page, attempt)
        if run_id is not None:
            events.emit(
                conn, run_id, "apply", "info",
                f"apply: {title} @ {company} via {channel_label or kind}: {outcome['status']}.",
            )
        return {"status": outcome["status"], "application_id": application_id, "reason": outcome.get("reason")}

    # unreachable: the last channel in the plan never falls through past itself
    return {"status": "failed", "application_id": application_id, "reason": "No channel left to try."}


def _attempt_page_channel(conn, application_id, page, target, merged_answers, cv_path, auto_submit: bool) -> dict:
    try:
        page.goto(target, wait_until="domcontentloaded")
        handler = pick_handler(target, page)
    except Exception as exc:  # noqa: BLE001 - a bad URL or a page-load crash must not crash the chain
        # Record what was attempted, so a failure on the last channel does
        # not leave the placeholder channel and the first channel's URL.
        known = pick_handler_by_url(target)
        attempted = _handler_channel(known, target) if known else _generic_channel(target)
        db.update_application(conn, application_id, channel=attempted, apply_url=target)
        return {"status": "failed", "reason": f"Could not open {target}: {exc}", "channel_label": None}

    channel_label = _handler_channel(handler, target)
    db.update_application(conn, application_id, channel=channel_label, apply_url=target)

    try:
        result = handler.apply(page, merged_answers, cv_path, auto_submit)
    except Exception as exc:  # noqa: BLE001 - a handler bug must never crash the chain
        return {
            "status": "failed",
            "reason": f"Apply handler raised an error: {exc}",
            "channel_label": channel_label,
        }

    return {
        "status": result.get("status", "needs_help"),
        "reason": result.get("reason"),
        "channel_label": channel_label,
    }


def _finalize_page_attempt(conn, config, job_id, application_id, page, attempt: dict) -> dict:
    status = attempt.get("status", "needs_help")
    reason = attempt.get("reason")
    if status == "sent":
        proof.capture_confirmation(conn, config, application_id, page)
    return _record_page_outcome(conn, job_id, application_id, status, reason)


def _record_page_outcome(conn, job_id, application_id, status, reason) -> dict:
    """Write a terminal page-attempt outcome to the application, job and help
    items. Shared by the server browser chain and the PC worker API."""
    if status == "sent":
        db.update_application(conn, application_id, status="sent")
        db.update_job_status(conn, job_id, "applied")
        db.resolve_open_help_items_for_job(conn, job_id, "Sent.")
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
    return {"status": status, "reason": reason}


def _attempt_email_channel(
    conn, config, job_id, application_id, to_email, job_facts, cover_letter, cv_path, auto_submit: bool
) -> dict:
    profile_answers = get_profile_answers(conn)
    draft = mailer.draft_email(
        to=to_email,
        job_title=job_facts["title"],
        profile_answers=profile_answers,
        cover_letter=cover_letter,
        cv_path=cv_path,
    )
    channel_label = f"email:{to_email.split('@', 1)[-1].lower()}"
    db.update_application(
        conn, application_id, channel=channel_label, apply_url=to_email,
        email_to=to_email, email_draft=json.dumps(draft),
    )

    if not auto_submit:
        db.update_application(conn, application_id, status="ready_to_submit")
        db.update_job_status(conn, job_id, "applying")
        return {"status": "ready_to_submit", "reason": None, "channel_label": channel_label}

    try:
        mailer.send_draft(conn, config, application_id, draft, cv_path)
    except mailer.SmtpNotConfigured:
        reason = "Email SMTP not configured. Run: uv run python -m autojob.set_smtp"
    except mailer.EmailSendError as exc:
        reason = f"Email send failed: {exc}"
    else:
        db.update_application(conn, application_id, status="sent")
        db.update_job_status(conn, job_id, "applied")
        return {"status": "sent", "reason": None, "channel_label": channel_label}

    db.update_application(conn, application_id, status="ready_to_submit", notes=reason)
    db.create_help_item(conn, job_id=job_id, reason=reason)
    db.update_job_status(conn, job_id, "needs_help")
    return {"status": "ready_to_submit", "reason": reason, "channel_label": channel_label}


def release_application(application_id: int) -> dict:
    """Sean releasing a `ready_to_submit` application from the dashboard. A
    held email is sent through `mailer.send_draft` with the draft already
    reviewed on the detail page (no browser, no new LLM call); anything else
    goes back to the same apply URL, refills it with the answers already
    saved on the application, and this time actually submits."""
    config = load_config()
    conn = db.get_connection(config)
    try:
        application = db.get_application(conn, application_id)
        if application is None:
            return {"status": "failed", "reason": "Application not found."}
        job = db.get_job_with_company(conn, application["job_id"])
        if job is None:
            return {"status": "failed", "reason": "Job not found."}

        if (application["channel"] or "").startswith("email:"):
            try:
                return _release_email_application(conn, config, application, job)
            except Exception as exc:  # noqa: BLE001 - releasing must never crash the caller
                reason = f"Release failed: {exc}"
                db.update_application(conn, application_id, status="failed", notes=reason)
                return {"status": "failed", "reason": reason}

        if (application["channel"] or "").startswith("agent:"):
            if not db.queue_worker_submit(conn, job["id"], application_id):
                return {
                    "status": "refused",
                    "reason": "A submit for this application is already queued or running on the PC worker.",
                }
            return {"status": "queued", "reason": "Queued for the PC worker's next run."}

        apply_url = application["apply_url"] or job["url"]
        visible_mode = _visible_mode(conn, config, None)
        try:
            with browser_page(config, headless=not visible_mode, visible=visible_mode) as page:
                page.goto(apply_url, wait_until="domcontentloaded")
                handler = pick_handler(apply_url, page)

                profile_answers = get_profile_answers(conn)
                answer_bank = get_answer_bank(conn)
                merged_answers = answers_module.build_answer_set(profile_answers, answer_bank)
                merged_answers["cover_letter"] = application["cover_letter"] or ""
                if application["answers_json"]:
                    merged_answers.update(json.loads(application["answers_json"]))
                merged_answers["cover_letter_file"] = str(
                    proof.save_cover_letter(config, application_id, merged_answers["cover_letter"])
                )
                merged_answers["_job_title"] = job["title"]
                merged_answers["_company_name"] = _row_get(job, "company_name", "") or ""
                merged_answers["_company_tldr"] = _row_get(job, "company_tldr", "") or ""
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


def _release_email_application(conn, config, application, job) -> dict:
    application_id = application["id"]
    job_id = job["id"]
    draft_raw = application["email_draft"]
    if not draft_raw:
        reason = "No email draft saved for this application."
        db.update_application(conn, application_id, status="failed", notes=reason)
        return {"status": "failed", "reason": reason}

    draft = json.loads(draft_raw)
    to_email = application["email_to"] or draft.get("to") or ""
    # Plan-time checks are not enough: two drafts can be held for one job
    # (Apply now twice, or a requeue after a failed send), and held drafts
    # do not count toward the daily cap until they go out.
    if db.email_already_sent(conn, job_id, to_email):
        reason = f"Not sent: an application email for this job was already sent to {to_email}."
        db.update_application(conn, application_id, status="discarded", notes=reason)
        return {"status": "failed", "reason": reason}
    cap = get_email_daily_cap(conn)
    if db.count_emails_sent_since(conn, _local_midnight_iso()) >= cap:
        reason = f"Not sent: the daily email cap ({cap}) is reached. Release again tomorrow."
        db.update_application(conn, application_id, status="ready_to_submit", notes=reason)
        return {"status": "failed", "reason": reason}

    cv_path = application["cv_path"]
    try:
        mailer.send_draft(conn, config, application_id, draft, cv_path)
    except mailer.SmtpNotConfigured:
        reason = "Email SMTP not configured. Run: uv run python -m autojob.set_smtp"
    except mailer.EmailSendError as exc:
        reason = f"Email send failed: {exc}"
    else:
        db.update_application(conn, application_id, status="sent")
        db.update_job_status(conn, job_id, "applied")
        db.resolve_open_help_items_for_job(conn, job_id, "Email sent on release.")
        return {"status": "sent", "reason": None}

    db.update_application(conn, application_id, status="ready_to_submit", notes=reason)
    # A retried Release that fails again reuses the open item instead of
    # stacking a new one per click.
    open_item = db.find_open_help_item(conn, job_id, reason_prefix="Email ")
    if open_item is None:
        db.create_help_item(conn, job_id=job_id, reason=reason)
    elif open_item["reason"] != reason:
        db.update_help_item_reason(conn, open_item["id"], reason)
    db.update_job_status(conn, job_id, "needs_help")
    return {"status": "ready_to_submit", "reason": reason}


def requeue_stale_no_handler_jobs(conn) -> int:
    """One-time maintenance: before `handlers/generic.py` existed, any job
    on an ATS none of the deterministic handlers recognized was escalated to
    `needs_help` with reason "No apply handler recognizes this ATS..."
    without ever being attempted. Now that the generic handler covers
    everything the router doesn't have a specific handler for, this resets
    those jobs to `queued` (so the next apply pass actually tries them) and
    deletes the now-stale help items.

    Idempotent: matches only that exact reason on a job still `needs_help`,
    so calling it again finds nothing left to do, and it never touches a
    help item that reflects a real outcome (a CAPTCHA, a login requirement,
    an unanswerable question). Does not run the apply pipeline itself:
    `auto_submit` still gates whether anything gets sent.

    Returns the number of jobs re-queued.
    """
    rows = conn.execute(
        """
        SELECT help_items.id AS help_item_id, jobs.id AS job_id
        FROM help_items
        JOIN jobs ON jobs.id = help_items.job_id
        WHERE help_items.resolved_at IS NULL
          AND jobs.status = 'needs_help'
          AND help_items.reason LIKE ?
        """,
        (f"%{STALE_NO_HANDLER_REASON_MARKER}%",),
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM help_items WHERE id = ?", (row["help_item_id"],))
        conn.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (row["job_id"],))
    conn.commit()
    return len(rows)

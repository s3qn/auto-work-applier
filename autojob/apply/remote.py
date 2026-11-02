"""Server side of the PC worker: claiming jobs, leases and attempt results.

The worker (`worker/`, runs on Sean's PC with a logged-in browser) drives the
page channels of a job. This module decides which jobs it gets
(`is_worker_job`), hands one out under a lease (`claim_next`), and turns each
reported attempt into the same application, job and help-item state the
server's own browser chain would have written (`record_attempt`). The
server keeps the fall-through decision and owns the email channel."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlparse

from autojob import db
from autojob.apply import answers as answers_module
from autojob.apply import apply as apply_module
from autojob.apply import proof
from autojob.apply.router import pick_handler_by_url
from autojob.config import Config
from autojob.llm.cv import latest_cv_pdf
from autojob.pipeline import events
from autojob.pipeline.schedule import TIMEZONE
from autojob.web.deps import get_auto_submit, get_profile_answers, get_answer_bank, get_targets

LEASE_MINUTES = 15
PILOT_SETTING = "worker_pilot"
PILOT_WINDOW_END = time(7, 0)
PILOT_APPLIED_STATUSES = ("ready_to_submit", "sent")
DESCRIPTION_LIMIT = 3000

STATUSES = ("filled", "submitted", "needs_help", "failed")
REASON_CODES = (
    "none", "captcha", "no_form", "login_required", "signup_required", "unanswerable",
    "cv_required", "easy_apply_only", "blocked", "agent_unavailable", "other",
)
SITE_BY_HOST = {"linkedin.com": "LinkedIn", "alljobs.co.il": "AllJobs", "jobmaster.co.il": "JobMaster"}

EASY_APPLY_ONLY_REASON = (
    "LinkedIn listing only offers Easy Apply, which this build never automates. Apply by hand: {url}"
)
REASON_TEXT = {
    "captcha": "CAPTCHA on form.",
    "no_form": "Could not locate an application form on this page.",
    "signup_required": "Account signup required before applying.",
    "blocked": "Could not open the application: {reason}",
    "agent_unavailable": "Apply attempt failed before a form could be filled: {reason}",
    "login_required": "{site} login required to apply.",
    "unanswerable": "Application form has question(s) with no honest basis to answer: {questions}",
    "cv_required": "CV upload required but no CV is uploaded. Upload one on the Profile page.",
    "easy_apply_only": EASY_APPLY_ONLY_REASON,
}


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _lease_until() -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(minutes=LEASE_MINUTES))


def _domain(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host or "unknown"


def _site_name(url: str) -> str:
    host = _domain(url)
    for suffix, name in SITE_BY_HOST.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return host


def page_channels(job, conn) -> list[tuple[str, str]]:
    """Page channels of the plan in plan order. Email is handled by the
    server after every page channel, never by the worker."""
    return [(kind, target) for kind, target in apply_module.plan_channels(job, conn) if kind != "email"]


def plan_email(job, conn) -> str | None:
    for kind, target in apply_module.plan_channels(job, conn):
        if kind == "email":
            return target
    return None


def is_worker_job(job, conn) -> bool:
    """A queued job belongs to the PC worker when it waits for a submit
    session or any page channel is not a known ATS."""
    if apply_module._row_get(job, "worker_phase") == "submit":
        return True
    return any(pick_handler_by_url(target) is None for _, target in page_channels(job, conn))


# --- pilot through the PC worker -------------------------------------------
#
# With a worker token, a pilot never applies on the server. `open_pilot`
# records the window in the `worker_pilot` setting ({"run_id", "cap",
# "until"}), and every fill the worker claims while that run is unfinished
# is bound to it: at most `cap` applications carry its run_id, and the
# worker is never allowed to submit. The run finishes once the cap is used
# and nothing is in flight, or at the first check after 07:00 local.


def pilot_window_end(now: datetime | None = None) -> str:
    """The next 07:00 Asia/Jerusalem after `now`, as a UTC ISO string."""
    local = (datetime.now(timezone.utc) if now is None else now).astimezone(TIMEZONE)
    end = datetime.combine(local.date(), PILOT_WINDOW_END, tzinfo=TIMEZONE)
    if end <= local:
        end = datetime.combine(local.date() + timedelta(days=1), PILOT_WINDOW_END, tzinfo=TIMEZONE)
    return _iso(end.astimezone(timezone.utc))


def _pilot_counts(conn, run_id: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS used, "
        "SUM(CASE WHEN a.status IN ('ready_to_submit', 'sent') THEN 1 ELSE 0 END) AS applied, "
        "SUM(CASE WHEN a.status = 'failed' THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN j.worker_lease_token IS NOT NULL AND j.worker_application_id = a.id "
        "THEN 1 ELSE 0 END) AS in_flight "
        "FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.run_id = ?",
        (run_id,),
    ).fetchone()
    return {k: int(row[k] or 0) for k in ("used", "applied", "failed", "in_flight")}


def finish_pilot(conn, run_id: int, why: str) -> None:
    run = db.get_run(conn, run_id)
    if run is not None and run["finished_at"] is None:
        counts = _pilot_counts(conn, run_id)
        log = (run["log"] or "") + f"\n[{db.now_iso()}] {why}"
        db.finish_run(
            conn, run_id, found=run["found"], filtered=run["filtered"],
            applied=counts["applied"], failed=counts["failed"], log=log.lstrip("\n"),
        )
        events.emit(conn, run_id, "system", "info", why)
        events.emit(conn, run_id, "system", "info", "Run finished.")
    current = db.get_setting(conn, PILOT_SETTING)
    if isinstance(current, dict) and current.get("run_id") == run_id:
        conn.execute("DELETE FROM settings WHERE key = ?", (PILOT_SETTING,))
        conn.commit()


def open_pilot(conn, run_id: int, cap: int, now: datetime | None = None) -> str:
    """Open the worker pilot window for `run_id`. A window still open from
    an earlier pilot is closed first. Returns the window end (UTC ISO)."""
    previous = db.get_setting(conn, PILOT_SETTING)
    if isinstance(previous, dict) and previous.get("run_id") not in (None, run_id):
        finish_pilot(conn, int(previous["run_id"]), f"Pilot window closed: pilot run {run_id} started.")
    until = pilot_window_end(now)
    db.set_setting(conn, PILOT_SETTING, {"run_id": run_id, "cap": int(cap), "until": until})
    return until


def active_pilot(conn) -> dict | None:
    """The open worker pilot window, or None. Finishes the run as a side
    effect when its window has ended or its cap is used up."""
    data = db.get_setting(conn, PILOT_SETTING)
    if not isinstance(data, dict) or not isinstance(data.get("run_id"), int):
        return None
    run_id = data["run_id"]
    run = db.get_run(conn, run_id)
    if run is None or run["finished_at"] is not None:
        finish_pilot(conn, run_id, "Pilot window closed.")
        return None
    cap = int(data.get("cap") or 0)
    if db.now_iso() >= str(data.get("until") or ""):
        finish_pilot(conn, run_id, "Pilot window ended at 07:00.")
        return None
    counts = _pilot_counts(conn, run_id)
    if counts["used"] >= cap and counts["in_flight"] == 0:
        finish_pilot(conn, run_id, f"Pilot cap of {cap} job(s) reached.")
        return None
    return {"run_id": run_id, "cap": cap, "until": data.get("until"), **counts}


def _is_pilot_application(conn, application) -> bool:
    run_id = application["run_id"] if application is not None else None
    if run_id is None:
        return False
    run = db.get_run(conn, run_id)
    return run is not None and run["mode"] == "pilot"


def _public_answers(merged: dict) -> dict:
    return {k: v for k, v in merged.items() if not k.startswith("_") and k != "cover_letter_file"}


def _job_payload(job) -> dict:
    return {
        "title": job["title"],
        "company": apply_module._row_get(job, "company_name", "") or "",
        "company_tldr": apply_module._row_get(job, "company_tldr", "") or "",
        "description": (apply_module._row_get(job, "description", "") or "")[:DESCRIPTION_LIMIT],
        "source": job["source"],
        "url": job["url"],
    }


def _cv_payload(config: Config) -> dict | None:
    cv = latest_cv_pdf(config)
    if cv is None:
        return None
    return {"filename": cv.name, "size": cv.stat().st_size}


def _build_fill_claim(conn, config: Config, job, run_id: int | None = None) -> dict | None:
    """Create the placeholder application for a fill session. Returns the
    claim body minus the lease fields, or None when there is no page
    channel to try."""
    pages = page_channels(job, conn)
    if not pages:
        return None
    profile_answers = get_profile_answers(conn)
    answer_bank = get_answer_bank(conn)
    merged = answers_module.build_answer_set(profile_answers, answer_bank)
    job_facts = apply_module._job_facts(job)
    company_tldr = apply_module._row_get(job, "company_tldr", "") or ""
    content = answers_module.generate_application_content(
        profile_answers, answer_bank, job_facts, company_tldr, work_dir=config.data_dir / "llm-work"
    )
    merged["cover_letter"] = content["cover_letter"]
    merged.update(content["screening_answers"])
    cv_path = latest_cv_pdf(config)
    application_id = db.create_application(
        conn,
        job_id=job["id"],
        channel="other",
        status="needs_help",  # placeholder, corrected by record_attempt
        cover_letter=content["cover_letter"],
        answers_json=json.dumps(content["screening_answers"]),
        cv_path=str(cv_path) if cv_path else None,
        apply_url=pages[0][1],
        run_id=run_id,
    )
    proof.save_cover_letter(config, application_id, content["cover_letter"])
    return {
        "application_id": application_id,
        "answers": _public_answers(merged),
        "cover_letter": content["cover_letter"],
        "channels": [{"index": i, "kind": k, "target": t} for i, (k, t) in enumerate(pages)],
    }


def _build_submit_claim(conn, config: Config, job) -> dict | None:
    application = db.get_application(conn, job["worker_application_id"]) if job["worker_application_id"] else None
    if application is None:
        return None
    merged = answers_module.build_answer_set(get_profile_answers(conn), get_answer_bank(conn))
    merged["cover_letter"] = application["cover_letter"] or ""
    if application["answers_json"]:
        merged.update(json.loads(application["answers_json"]))
    target = application["apply_url"] or job["url"]
    kind = next((k for k, t in page_channels(job, conn) if t == target), "job_url")
    return {
        "application_id": application["id"],
        "answers": _public_answers(merged),
        "cover_letter": application["cover_letter"] or "",
        "channels": [{"index": 0, "kind": kind, "target": target}],
    }


def claim_next(conn, config: Config, worker: str = "") -> dict | None:
    """Lease the next job for the PC worker, or None when nothing is
    claimable. Submit sessions (a Release click) come before fresh fills.
    While a worker pilot window is open, only pilot fills are handed out:
    any queued job (the server applies nothing during it), at most the
    pilot cap, never with submit allowed."""
    db.expire_worker_leases(conn, db.now_iso())
    pilot = active_pilot(conn)
    queued = db.list_jobs_with_company(conn, status="queued")
    if pilot is not None:
        if pilot["used"] >= pilot["cap"]:
            return None
        submits = []
        fills = [j for j in queued if j["worker_phase"] != "submit"]
        cap = None
    else:
        submits = [j for j in queued if j["worker_phase"] == "submit"]
        fills = [j for j in queued if j["worker_phase"] != "submit" and is_worker_job(j, conn)]
        cap = int(get_targets(conn).get("daily_cap_per_source", 20))
    today_start = f"{db.now_iso()[:10]}T00:00:00Z"
    counted: dict[str, int] = {}

    for job in submits + fills:
        phase = "submit" if job["worker_phase"] == "submit" else "fill"
        if phase == "fill" and cap is not None:
            source = job["source"]
            if source not in counted:
                counted[source] = db.count_applications_since(conn, source=source, since_iso=today_start)
            if counted[source] >= cap:
                continue
        try:
            if phase == "submit":
                built = _build_submit_claim(conn, config, job)
            else:
                built = _build_fill_claim(conn, config, job, run_id=pilot["run_id"] if pilot else None)
        except Exception:  # noqa: BLE001 - one bad job must not block the queue
            continue
        if built is None:
            continue
        lease_token = secrets.token_urlsafe(16)
        lease_until = _lease_until()
        if not db.claim_job(
            conn, job["id"], lease_until=lease_until, lease_token=lease_token, phase=phase,
            application_id=built["application_id"],
        ):
            if phase == "fill":
                db.delete_application(conn, built["application_id"])
            continue
        if pilot is not None:
            submit_allowed = False
            events.emit(
                conn, pilot["run_id"], "apply", "info",
                f"PC worker claimed {job['title']} ({pilot['used'] + 1} of {pilot['cap']}).",
            )
        else:
            submit_allowed = True if phase == "submit" else get_auto_submit(conn)
        return {
            "job_id": job["id"],
            "application_id": built["application_id"],
            "lease_token": lease_token,
            "lease_until": lease_until,
            "phase": phase,
            "submit_allowed": submit_allowed,
            "pilot_run_id": pilot["run_id"] if pilot else None,
            "job": _job_payload(job),
            "channels": built["channels"],
            "answers": built["answers"],
            "cover_letter": built["cover_letter"],
            "cv": _cv_payload(config),
        }
    return None


def result_for(payload: dict) -> dict:
    """The server's canonical {"status", "reason"} for a reported attempt."""
    status = payload["status"]
    code = payload.get("reason_code") or "none"
    detail = (payload.get("reason") or "").strip()
    target = payload.get("final_url") or payload.get("target") or ""
    unanswered = [str(q) for q in (payload.get("unanswered_required") or []) if str(q).strip()]
    if unanswered and status in ("filled", "needs_help"):
        status, code = "needs_help", "unanswerable"
    if status == "filled":
        return {"status": "ready_to_submit", "reason": None}
    if status == "submitted":
        return {"status": "sent", "reason": None}
    if code == "none":
        code = "other"
    template = REASON_TEXT.get(code)
    if template is None:
        return {"status": status, "reason": detail or "The PC worker reported a problem."}
    reason = template.format(
        reason=detail or "no detail given",
        site=_site_name(target),
        questions="; ".join(unanswered[:5]),
        url=target,
    )
    return {"status": status, "reason": reason}


def validate_payload(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if not isinstance(payload.get("index"), int) or payload["index"] < 0:
        raise ValueError("index must be a non-negative integer")
    if payload.get("status") not in STATUSES:
        raise ValueError("unknown status")
    if (payload.get("reason_code") or "none") not in REASON_CODES:
        raise ValueError("unknown reason_code")
    if not isinstance(payload.get("target"), str) or not payload["target"]:
        raise ValueError("target is required")


def _notes_line(payload: dict) -> str:
    fields = payload.get("fields_filled") or []
    rendered = "; ".join(f"{f.get('label')}={f.get('value')}" for f in fields if isinstance(f, dict))
    return (
        f"Agent filled {len(fields)} field(s): {rendered}; "
        f"CV uploaded: {'yes' if payload.get('cv_uploaded') else 'no'}; "
        f"denied submit attempts: {int(payload.get('denied_tool_calls') or 0)}"
    )


def record_attempt(conn, config: Config, job, application_id: int, payload: dict) -> dict:
    """Apply one reported attempt. Returns {"next_index": n} when the worker
    should try another page channel, else {"done": True, "status": ...}."""
    validate_payload(payload)
    job_id = job["id"]
    application = db.get_application(conn, application_id)
    lines = [line for line in (application["notes"] or "").split("\n") if line]
    index = payload["index"]
    kind = payload.get("kind") or "job_url"
    target = payload["target"]
    submit_phase = apply_module._row_get(job, "worker_phase") == "submit"
    pilot = _is_pilot_application(conn, application)

    result = result_for(payload)
    pages = page_channels(job, conn)
    email = plan_email(job, conn)
    is_last = submit_phase or (index >= len(pages) - 1 and email is None)
    channel_label = f"agent:{_domain(payload.get('final_url') or target)}"
    db.update_application(conn, application_id, channel=channel_label, apply_url=target)
    lines.append(_notes_line(payload))

    if apply_module._should_fall_through(result, is_last):
        lines.append(f"Tried {kind} {target}: {result['reason']}")
        db.update_application(conn, application_id, notes="\n".join(lines))
        if index + 1 < len(pages):
            return {"next_index": index + 1}
        if email:
            auto_submit = False if pilot else get_auto_submit(conn)
            apply_module._attempt_email_channel(
                conn, config, job_id, application_id, email, apply_module._job_facts(job),
                application["cover_letter"] or "", application["cv_path"], auto_submit,
            )
            db.clear_lease(conn, job_id)
            status = db.get_application(conn, application_id)["status"]
            _after_pilot_attempt(conn, application, job, status)
            return {"done": True, "status": status}

    outcome = apply_module._record_page_outcome(conn, job_id, application_id, result["status"], result["reason"])
    if result["reason"] and result["status"] in ("needs_help", "failed"):
        lines.append(result["reason"])
    db.update_application(conn, application_id, notes="\n".join(lines))
    db.clear_lease(conn, job_id)
    _after_pilot_attempt(conn, application, job, outcome["status"])
    return {"done": True, "status": outcome["status"]}


def _after_pilot_attempt(conn, application, job, status: str) -> None:
    """Keep a pilot run's counts current and finish it once its cap is used."""
    if not _is_pilot_application(conn, application):
        return
    run_id = application["run_id"]
    counts = _pilot_counts(conn, run_id)
    db.update_run_counts(conn, run_id, applied=counts["applied"], failed=counts["failed"])
    events.bump_progress(
        conn, run_id, "apply", done=counts["used"] - counts["in_flight"], applied=counts["applied"]
    )
    events.emit(conn, run_id, "apply", "info", f"PC worker: {job['title']}: {status}.")
    active_pilot(conn)

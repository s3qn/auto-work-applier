"""The daily apply queue: a stored batch of jobs the PC session pulls, and
the bookkeeping for the results it posts back. No browser here."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

from autojob import db
from autojob.apply import answers as answers_module
from autojob.apply import apply as apply_module
from autojob.apply.trim import short_variant, trim_cover_letter
from autojob.config import Config
from autojob.llm.cv import latest_cv_pdf
from autojob.web.deps import get_answer_bank, get_profile_answers

DEFAULT_QUEUE_SIZE = 20
DEFAULT_MAX_RETRIES = 2
WEBSITE = "https://sean.build"
# Batch creation can call the claude CLI: one at a time keeps two pulls
# from building two batches for the same day.
BATCH_LOCK = threading.Lock()


def queue_size(conn) -> int:
    value = db.get_setting(conn, "apply_queue_size", DEFAULT_QUEUE_SIZE)
    try:
        size = int(value)
    except (TypeError, ValueError):
        return DEFAULT_QUEUE_SIZE
    return size if 1 <= size <= 50 else max(1, min(50, size))


def max_retries(conn) -> int:
    try:
        return int(db.get_setting(conn, "apply_queue_max_retries", DEFAULT_MAX_RETRIES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RETRIES


def cv_version(config: Config) -> str | None:
    cv = latest_cv_pdf(config)
    return f"{cv.name}@{int(cv.stat().st_mtime)}" if cv else None


def site_for(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    for name in ("linkedin", "jobmaster", "alljobs"):
        if name in host:
            return name
    return "company"


def _answers(conn) -> dict:
    merged = answers_module.build_answer_set(get_profile_answers(conn), get_answer_bank(conn))
    merged["website"] = WEBSITE
    merged["years_experience"] = answers_module.DEFAULT_ANSWER_BANK["years_experience"]
    return merged


def _existing_letter(conn, job_id: int) -> str | None:
    row = conn.execute(
        "SELECT cover_letter FROM applications WHERE job_id = ? AND TRIM(COALESCE(cover_letter, '')) != '' "
        "ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return row["cover_letter"] if row else None


def _apply_url(job, conn) -> str:
    for kind, target in apply_module.plan_channels(job, conn):
        if kind != "email":
            return target
    return job["url"]


def build_batch(conn, config: Config, day: str) -> int | None:
    size = queue_size(conn)
    selected = db.select_apply_batch_jobs(conn, size)
    if not selected:
        return None
    profile = get_profile_answers(conn)
    bank = get_answer_bank(conn)
    answers = _answers(conn)
    jobs = [db.get_job_with_company(conn, row["id"]) for row in selected]
    letters = {job["id"]: _existing_letter(conn, job["id"]) for job in jobs}

    def generate(job):
        content = answers_module.generate_application_content(
            profile, bank, apply_module._job_facts(job),
            apply_module._row_get(job, "company_tldr", "") or "",
            work_dir=config.data_dir / "llm-work",
        )
        return job["id"], content["cover_letter"]

    missing = [job for job in jobs if not letters[job["id"]]]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for job_id, letter in pool.map(generate, missing):
            letters[job_id] = letter

    items = []
    for job in jobs:
        apply_url = _apply_url(job, conn)
        full = trim_cover_letter(letters[job["id"]] or "")
        payload = {
            "id": job["id"],
            "title": job["title"],
            "company": apply_module._row_get(job, "company_name", "") or "",
            "location": job["location"],
            "fit": job["fit_score"],
            "apply_url": apply_url,
            "site": site_for(apply_url),
            "answers": answers,
            "cover_letter": full,
            "cover_letter_short": short_variant(full),
        }
        items.append((job["id"], json.dumps(payload)))
    return db.create_batch(conn, day, items)


def get_or_create_today(conn, config: Config) -> dict:
    """Today's batch response. Creates and freezes it on the first call."""
    day = db.jerusalem_day()
    with BATCH_LOCK:
        db.expire_open_batch_items_before(conn, day)
        batch = db.get_batch_for_day(conn, day)
        batch_id = batch["id"] if batch else build_batch(conn, config, day)
        if batch_id is None:
            return {"batch_id": None, "day": day, "size": 0, "jobs": [],
                    "cv_version": cv_version(config)}
        db.touch_batch_pull(conn, batch_id)
        items = db.list_batch_items(conn, batch_id)
        jobs = [json.loads(i["payload_json"]) for i in items if i["state"] == "open"]
    return {"batch_id": batch_id, "day": day, "size": len(items), "jobs": jobs,
            "cv_version": cv_version(config)}


def prebuild_today(conn, config: Config) -> int:
    """Build today's batch without touching `last_pulled_at`. Returns the job count."""
    day = db.jerusalem_day()
    with BATCH_LOCK:
        db.expire_open_batch_items_before(conn, day)
        if db.get_batch_for_day(conn, day):
            return 0
        batch_id = build_batch(conn, config, day)
        return len(db.list_batch_items(conn, batch_id)) if batch_id else 0


def _timestamp(value: str | None) -> str:
    """Normalized UTC string. Raises ValueError for an unparseable or out of range value."""
    if value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"bad timestamp: {value!r}") from exc
    return db.now_iso()


def record_result(conn, *, job_id: int, status: str, reason: str | None, cover_letter_used: str | None,
                  cv_version_used: str | None, timestamp: str | None) -> dict | None:
    """Apply one result from the PC. None for an unknown job, ValueError for a bad timestamp."""
    job = db.get_job(conn, job_id)
    if job is None:
        return None
    when = _timestamp(timestamp)
    reason = (reason or "").strip() or None
    item = db.latest_batch_item(conn, job_id)
    application = conn.execute(
        "SELECT * FROM applications WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()

    if application is not None and application["status"] in ("sent", "replied", "interview", "rejected"):
        return {"ok": True, "duplicate": True}
    if item is not None and status != "submitted" and item["state"] == status and (item["reason"] or None) == reason:
        return {"ok": True, "duplicate": True}

    payload = json.loads(item["payload_json"]) if item is not None else {}
    apply_url = payload.get("apply_url") or job["url"]
    site = payload.get("site") or site_for(apply_url)
    line = f"PC {status} {when}" + (f": {reason}" if reason else "")

    if application is None:
        app_id = db.create_application(
            conn, job_id=job_id, channel=f"pc:{site}", status="failed", apply_url=apply_url, notes=line,
        )
        conn.execute("UPDATE applications SET applied_at = ? WHERE id = ?", (when, app_id))
    else:
        app_id = application["id"]
        notes = ((application["notes"] or "") + "\n" + line).strip()
        conn.execute("UPDATE applications SET notes = ?, applied_at = ? WHERE id = ?", (notes, when, app_id))

    if cover_letter_used:
        conn.execute("UPDATE applications SET cover_letter = ? WHERE id = ?", (cover_letter_used, app_id))
    if cv_version_used:
        conn.execute("UPDATE applications SET cv_version = ? WHERE id = ?", (cv_version_used, app_id))

    if status == "submitted":
        conn.execute("UPDATE applications SET status = 'sent' WHERE id = ?", (app_id,))
        conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (job_id,))
    elif status == "blocked":
        vip = "vip" in (reason or "").lower() or "CreditCard.aspx" in (apply_url or "")
        stored = f"vip: {reason or 'blocked'}" if vip else (reason or "blocked")
        conn.execute("UPDATE applications SET status = 'failed' WHERE id = ?", (app_id,))
        conn.execute("UPDATE jobs SET status = 'failed', reason = ? WHERE id = ?", (stored, job_id))
    else:
        attempts = (job["pc_attempts"] or 0) + 1
        next_status = "queued" if attempts <= max_retries(conn) else "failed"
        conn.execute("UPDATE applications SET status = 'failed' WHERE id = ?", (app_id,))
        conn.execute("UPDATE jobs SET pc_attempts = ?, status = ? WHERE id = ?", (attempts, next_status, job_id))
    conn.commit()
    if item is not None:
        db.record_batch_result(conn, item["batch_id"], job_id, status, reason)
    return {"ok": True}

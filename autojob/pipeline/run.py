"""Scan pipeline entry point.

run_scan() searches the configured job sources for Sean's target
titles/keywords, dedups against what is already in the database, screens
the new jobs with the local claude CLI (haiku), researches the companies
behind the jobs it keeps (sonnet, with web search), and records everything
so the dashboard can show it. It then sends every currently queued job
(from this run or a previous one) through the apply pipeline, respecting
the daily cap per source from the Targets page and the `auto_submit`
safety default. One bad source, one bad job, one broken LLM call, or one
failed apply attempt never crashes the whole run: everything is caught,
logged to the run's log, and the run continues.
"""

from __future__ import annotations

import sys
from pathlib import Path

from autojob import db
from autojob.apply import remote
from autojob.apply.apply import apply_to_job
from autojob.config import load_config
from autojob.llm import filter as llm_filter
from autojob.llm.cv import get_cv_text
from autojob.pipeline import events
from autojob.pipeline.lock import RunInProgress, hold_run_lock
from autojob.pipeline.research import research_pending
from autojob.sources import SourceError, alljobs, jobmaster, linkedin
from autojob.worker_token import worker_configured
from autojob.web.deps import get_auto_submit, get_targets

MAX_RESEARCH_PER_RUN = 25
LINKEDIN_MAX_PAGES = 2
LINKEDIN_MAX_DETAIL_FETCHES = 15
SOURCES = ("linkedin", "alljobs", "jobmaster")
DEFAULT_DAILY_CAP_PER_SOURCE = 20
RUN_MODES = ("full", "refresh", "pilot")
DEFAULT_PILOT_CAP = 20
APPLIED_STATUSES = ("sent", "ready_to_submit")


class RunLogger:
    """Collects human-readable log lines and persists them to the run row
    after each one, so the run detail page can poll and show live progress
    while a run is still going."""

    def __init__(self, conn, run_id: int) -> None:
        self._conn = conn
        self._run_id = run_id
        self._lines: list[str] = []

    def log(self, line: str) -> None:
        self._lines.append(f"[{db.now_iso()}] {line}")
        db.update_run_log(self._conn, self._run_id, self.text)

    @property
    def text(self) -> str:
        return "\n".join(self._lines)


def _search_source(
    source_name: str,
    keyword: str,
    *,
    linkedin_max_pages: int,
    linkedin_max_detail_fetches: int,
) -> list[dict]:
    if source_name == "linkedin":
        return linkedin.search(
            keyword, max_pages=linkedin_max_pages, max_detail_fetches=linkedin_max_detail_fetches
        )
    if source_name == "alljobs":
        return alljobs.search(keyword)
    if source_name == "jobmaster":
        return jobmaster.search(keyword)
    raise ValueError(f"unknown source {source_name!r}")


def _dedup_new_jobs(conn, raw_jobs: list[dict]) -> list[dict]:
    """Drop jobs already in the database (by URL, external id, or the
    normalized company+title dedup key) and duplicates within this run."""
    new_jobs: list[dict] = []
    seen_urls: set[str] = set()
    seen_keys: set[str] = set()
    for job in raw_jobs:
        url = (job.get("url") or "").strip()
        title = (job.get("title") or "").strip()
        if not url or not title:
            continue
        company_name = (job.get("company") or "Unknown").strip() or "Unknown"
        dedup_key = db.normalize_dedup_key(company_name, title)
        external_id = job.get("external_id")
        if url in seen_urls or dedup_key in seen_keys:
            continue
        if db.job_url_exists(conn, url):
            continue
        if external_id and db.job_external_id_exists(conn, job["source"], external_id):
            continue
        if conn.execute("SELECT id FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone():
            continue
        seen_urls.add(url)
        seen_keys.add(dedup_key)
        new_jobs.append(job)
    return new_jobs


def _reap_interrupted(conn, logger, run_id: int) -> None:
    # A pilot window the PC worker is still working through is open on
    # purpose; active_pilot finishes it here if it is past due.
    pilot = remote.active_pilot(conn)
    keep = (pilot["run_id"],) if pilot else ()
    closed = db.close_interrupted_runs(conn, closed_by_run_id=run_id, keep_ids=keep)
    if closed:
        logger.log(f"Closed {closed} interrupted run(s).")
    requeued = db.requeue_interrupted_applying_jobs(conn)
    if requeued:
        logger.log(f"Requeued {requeued} job(s) left in 'applying' by an interrupted run.")


def run_scan(
    trigger: str = "manual",
    *,
    mode: str = "full",
    keywords: list[str] | None = None,
    max_research: int = MAX_RESEARCH_PER_RUN,
    linkedin_max_pages: int = LINKEDIN_MAX_PAGES,
    linkedin_max_detail_fetches: int = LINKEDIN_MAX_DETAIL_FETCHES,
    claim_pilot_tonight: bool = False,
) -> int | None:
    """Run one scan cycle and record it. Returns the new run id.

    `keywords` overrides the titles/keywords from the Targets page, mainly
    for a smaller, faster manual run. Everything else always reads from the
    real settings and database.

    `mode="refresh"` runs scan and filter only; research and apply are
    skipped. `mode="pilot"` runs only the apply phase with auto_submit forced
    off and at most `pilot_cap` jobs. Raises RunInProgress if another run holds the run lock.
    `claim_pilot_tonight` clears the pilot_tonight toggle once the lock is held; if the toggle was
    already off (or another process claimed it) nothing runs and None is returned.
    """
    if mode not in RUN_MODES:
        raise ValueError(f"Unknown run mode: {mode!r}")
    config = load_config()
    with hold_run_lock(config):
        if claim_pilot_tonight:
            db.init_db(config)
            conn = db.get_connection(config)
            try:
                if not db.claim_setting_flag(conn, "pilot_tonight"):
                    return None
            finally:
                conn.close()
        return _run_scan_locked(
            config, trigger, mode, keywords, max_research, linkedin_max_pages, linkedin_max_detail_fetches
        )


def _run_scan_locked(
    config, trigger, mode, keywords, max_research, linkedin_max_pages, linkedin_max_detail_fetches
) -> int:
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger=trigger, mode=mode)
        logger = RunLogger(conn, run_id)
        _reap_interrupted(conn, logger, run_id)
        events.emit(conn, run_id, "system", "info", f"Run started ({trigger}).")
        found = 0
        filtered = 0
        applied = 0
        failed = 0
        if mode == "pilot":
            if worker_configured(config):
                cap = get_pilot_cap(conn)
                until = remote.open_pilot(conn, run_id, cap)
                message = (
                    f"Pilot through the PC worker: up to {cap} job(s), nothing is submitted. "
                    f"The window closes when the cap is used or at 07:00 ({until})."
                )
                logger.log(message)
                events.emit(conn, run_id, "system", "info", message)
                events.bump_progress(conn, run_id, "apply", done=0, total=cap)
                return run_id
            try:
                cap = get_pilot_cap(conn)
                logger.log(f"Pilot: apply only, nothing is submitted, up to {cap} job(s).")
                events.emit(conn, run_id, "system", "info", f"Pilot: apply only, nothing is submitted, up to {cap} job(s).")
                applied, failed = _run_apply_phase(
                    conn, logger, daily_cap=None, auto_submit=False, run_id=run_id, limit=cap
                )
            except Exception as exc:  # noqa: BLE001 - the run must always finish and record something
                logger.log(f"Run failed unexpectedly: {exc}")
                events.emit(conn, run_id, "system", "error", f"Run failed unexpectedly: {exc}")
                failed += 1
            db.finish_run(conn, run_id, found=found, filtered=filtered, applied=applied, failed=failed, log=logger.text)
            events.emit(conn, run_id, "system", "info", "Run finished.")
            return run_id
        try:
            targets = get_targets(conn)
            search_keywords = keywords if keywords is not None else (targets.get("titles") or [])
            if not search_keywords:
                logger.log("No target titles/keywords configured. Add some on the Targets page.")
                events.emit(
                    conn, run_id, "system", "info",
                    "No target titles/keywords configured. Add some on the Targets page.",
                )

            raw_jobs: list[dict] = []
            scan_total = len(SOURCES) * len(search_keywords)
            scan_done = 0
            for source_name in SOURCES:
                source_count = 0
                blocked = False
                for keyword in search_keywords:
                    try:
                        results = _search_source(
                            source_name,
                            keyword,
                            linkedin_max_pages=linkedin_max_pages,
                            linkedin_max_detail_fetches=linkedin_max_detail_fetches,
                        )
                    except SourceError as exc:
                        logger.log(f"{source_name}: blocked or unusable for '{keyword}': {exc}")
                        events.emit(
                            conn, run_id, "scan", "error",
                            f"{source_name}: blocked or unusable for '{keyword}': {exc}",
                        )
                        blocked = True
                        scan_done += 1
                        events.bump_progress(conn, run_id, "scan", done=scan_done, total=scan_total)
                        continue
                    except Exception as exc:  # noqa: BLE001 - one source must never crash the run
                        logger.log(f"{source_name}: unexpected error for '{keyword}': {exc}")
                        events.emit(
                            conn, run_id, "scan", "error",
                            f"{source_name}: unexpected error for '{keyword}': {exc}",
                        )
                        failed += 1
                        scan_done += 1
                        events.bump_progress(conn, run_id, "scan", done=scan_done, total=scan_total)
                        continue
                    source_count += len(results)
                    raw_jobs.extend(results)
                    scan_done += 1
                    events.bump_progress(
                        conn, run_id, "scan",
                        f"{source_name}: '{keyword}' returned {len(results)} job(s).",
                        done=scan_done, total=scan_total,
                    )
                status = " (some keywords blocked)" if blocked and source_count else ""
                logger.log(
                    f"{source_name}: {source_count} job(s) across {len(search_keywords)} keyword(s){status}."
                )
                events.emit(
                    conn, run_id, "scan", "info",
                    f"{source_name}: {source_count} job(s) across {len(search_keywords)} keyword(s){status}.",
                )

            logger.log(f"Total raw results before dedup: {len(raw_jobs)}.")
            new_jobs = _dedup_new_jobs(conn, raw_jobs)
            found = len(new_jobs)
            logger.log(f"{found} new job(s) after dedup.")
            db.update_run_counts(conn, run_id, found=found)
            events.bump_progress(
                conn, run_id, "scan", f"{found} new job(s) after dedup.",
                done=scan_total, total=scan_total, found=found,
            )

            if new_jobs:
                work_dir = config.data_dir / "llm-work"
                cv_text = get_cv_text(config)
                if not cv_text:
                    logger.log("No CV on file yet; filtering will run without CV context.")
                    events.emit(conn, run_id, "filter", "info", "No CV on file yet; filtering will run without CV context.")

                filter_results = llm_filter.filter_jobs(new_jobs, cv_text, work_dir=work_dir, conn=conn, run_id=run_id)

                status_counts: dict[str, int] = {}
                for job, result in zip(new_jobs, filter_results):
                    try:
                        company_name = (job.get("company") or "Unknown").strip() or "Unknown"
                        company_id = db.get_or_create_company(conn, company_name)
                        dedup_key = db.normalize_dedup_key(company_name, job.get("title", ""))
                        job_id = db.create_job(
                            conn,
                            source=job["source"],
                            url=job["url"],
                            title=job.get("title", ""),
                            company_id=company_id,
                            location=job.get("location") or "",
                            description=job.get("description") or "",
                            status=result["status"],
                            fit_score=result["fit_score"],
                            reason=result["reason"],
                            external_id=job.get("external_id"),
                            dedup_key=dedup_key,
                        )
                    except Exception as exc:  # noqa: BLE001 - one bad job must never stop the rest
                        logger.log(f"Could not store job {job.get('url', '(no url)')}: {exc}")
                        events.emit(conn, run_id, "filter", "error", f"Could not store job {job.get('url', '(no url)')}: {exc}")
                        failed += 1
                        continue
                    status_counts[result["status"]] = status_counts.get(result["status"], 0) + 1

                filtered = status_counts.get("filtered_out", 0)
                logger.log(
                    "Filter results: "
                    f"{status_counts.get('queued', 0)} queued, "
                    f"{filtered} filtered out, "
                    f"{status_counts.get('new', 0)} left as new (filter failed)."
                )
                db.update_run_counts(conn, run_id, filtered=filtered)
                events.bump_progress(
                    conn, run_id, "filter",
                    f"Filter results: {status_counts.get('queued', 0)} queued, {filtered} filtered out.",
                    done=len(new_jobs), total=len(new_jobs), filtered=filtered,
                )
            else:
                logger.log("Nothing new to filter.")
                events.emit(conn, run_id, "system", "info", "Nothing new to filter.")

            if mode == "full":
                # Every scan, not only one that found something: companies
                # already in the database still need their apply channels
                # looked up, and a failed lookup gets retried here.
                _, research_failed = research_pending(
                    conn, work_dir=config.data_dir / "llm-work", limit=max_research, run_id=run_id, logger=logger
                )
                failed += research_failed

                auto_submit = get_auto_submit(conn)
                daily_cap = targets.get("daily_cap_per_source") or DEFAULT_DAILY_CAP_PER_SOURCE
                apply_applied, apply_failed = _run_apply_phase(
                    conn, logger, daily_cap=daily_cap, auto_submit=auto_submit, run_id=run_id
                )
                applied += apply_applied
                failed += apply_failed
            else:
                logger.log("Refresh: research and apply skipped.")
                events.emit(conn, run_id, "system", "info", "Refresh: research and apply skipped.")
        except Exception as exc:  # noqa: BLE001 - the run must always finish and record something
            logger.log(f"Run failed unexpectedly: {exc}")
            events.emit(conn, run_id, "system", "error", f"Run failed unexpectedly: {exc}")
            failed += 1

        db.finish_run(conn, run_id, found=found, filtered=filtered, applied=applied, failed=failed, log=logger.text)
        events.emit(conn, run_id, "system", "info", "Run finished.")
        return run_id
    finally:
        conn.close()


def get_pilot_cap(conn) -> int:
    try:
        cap = int(db.get_setting(conn, "pilot_cap", DEFAULT_PILOT_CAP))
    except (TypeError, ValueError):
        return DEFAULT_PILOT_CAP
    return min(max(cap, 1), 100)


def _run_apply_phase(
    conn, logger: RunLogger, *, daily_cap: int | None, auto_submit: bool, run_id: int | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """Send every currently queued job (found this run or an earlier one)
    through `apply_to_job`, respecting `daily_cap` applications per source
    per day. Returns (applied_count, failed_count). One bad job never stops
    the rest: a crash from `apply_to_job` itself is caught here too, even
    though `apply_to_job` already catches its own internal failures."""
    applied = 0
    failed = 0
    today_start = f"{db.now_iso()[:10]}T00:00:00Z"
    sent_today_by_source: dict[str, int] = {}
    queued_jobs = db.list_jobs_with_company(conn, status="queued")
    if worker_configured(load_config()):
        # Jobs that need a logged-in browser belong to the PC worker.
        mine = [job for job in queued_jobs if not remote.is_worker_job(job, conn)]
        left = len(queued_jobs) - len(mine)
        if left:
            logger.log(f"{left} job(s) left for the PC worker.")
            if run_id is not None:
                events.emit(conn, run_id, "apply", "info", f"{left} job(s) left for the PC worker.")
        queued_jobs = mine
    if limit is not None:
        queued_jobs = queued_jobs[:limit]
    if not queued_jobs:
        return applied, failed

    total = len(queued_jobs)
    done = 0
    cap_text = f"daily cap {daily_cap} per source" if daily_cap is not None else "no daily cap"
    logger.log(f"Apply phase: {total} job(s) queued, {cap_text}.")
    if run_id is not None:
        events.emit(conn, run_id, "apply", "info", f"Apply phase: {total} job(s) queued, {cap_text}.")
    for job in queued_jobs:
        source = job["source"]
        if source not in sent_today_by_source:
            sent_today_by_source[source] = db.count_applications_since(conn, source=source, since_iso=today_start)
        if daily_cap is not None and sent_today_by_source[source] >= daily_cap:
            done += 1
            if run_id is not None:
                events.bump_progress(conn, run_id, "apply", done=done, total=total, applied=applied)
            continue
        try:
            result = apply_to_job(job, auto_submit=auto_submit, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - one bad job must never stop the apply phase
            logger.log(f"Apply attempt crashed for job {job['id']} ({job['title']}): {exc}")
            if run_id is not None:
                events.emit(conn, run_id, "apply", "error", f"apply: crashed for {job['title']}: {exc}")
            failed += 1
            done += 1
            if run_id is not None:
                events.bump_progress(conn, run_id, "apply", done=done, total=total, applied=applied)
            continue
        status = result.get("status")
        done += 1
        if status in APPLIED_STATUSES:
            applied += 1
            sent_today_by_source[source] = sent_today_by_source[source] + 1
            logger.log(f"Applied to job {job['id']} ({job['title']}) via {source}: {status}.")
        elif status == "needs_help":
            logger.log(f"Job {job['id']} ({job['title']}) needs help: {result.get('reason')}")
        else:
            failed += 1
            logger.log(f"Apply failed for job {job['id']} ({job['title']}): {result.get('reason')}")
        if run_id is not None:
            db.update_run_counts(conn, run_id, applied=applied)
            events.bump_progress(conn, run_id, "apply", done=done, total=total, applied=applied)
    return applied, failed


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = "full"
    if "--pilot" in args:
        mode = "pilot"
    try:
        run_id = run_scan(trigger="scheduled", mode=mode, claim_pilot_tonight=(mode == "pilot"))
    except RunInProgress:
        print("Another run is in progress; skipping this scheduled run.")
        return 0
    if run_id is None:
        print("Pilot tonight is off; nothing to do.")
        return 0
    print(f"Run {run_id} finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

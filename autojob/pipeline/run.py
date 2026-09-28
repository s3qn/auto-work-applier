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

from pathlib import Path

from autojob import db
from autojob.apply.apply import apply_to_job
from autojob.config import load_config
from autojob.llm import filter as llm_filter
from autojob.llm import research as llm_research
from autojob.llm.cv import get_cv_text
from autojob.sources import SourceError, alljobs, jobmaster, linkedin
from autojob.web.deps import get_auto_submit, get_targets

MAX_RESEARCH_PER_RUN = 25
LINKEDIN_MAX_PAGES = 2
LINKEDIN_MAX_DETAIL_FETCHES = 15
SOURCES = ("linkedin", "alljobs", "jobmaster")
DEFAULT_DAILY_CAP_PER_SOURCE = 20
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


def run_scan(
    trigger: str = "manual",
    *,
    keywords: list[str] | None = None,
    max_research: int = MAX_RESEARCH_PER_RUN,
    linkedin_max_pages: int = LINKEDIN_MAX_PAGES,
    linkedin_max_detail_fetches: int = LINKEDIN_MAX_DETAIL_FETCHES,
) -> int:
    """Run one scan cycle and record it. Returns the new run id.

    `keywords` overrides the titles/keywords from the Targets page, mainly
    for a smaller, faster manual run. Everything else always reads from the
    real settings and database.
    """
    config = load_config()
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        run_id = db.create_run(conn, trigger=trigger)
        logger = RunLogger(conn, run_id)
        found = 0
        filtered = 0
        applied = 0
        failed = 0
        try:
            targets = get_targets(conn)
            search_keywords = keywords if keywords is not None else (targets.get("titles") or [])
            if not search_keywords:
                logger.log("No target titles/keywords configured. Add some on the Targets page.")

            raw_jobs: list[dict] = []
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
                        blocked = True
                        continue
                    except Exception as exc:  # noqa: BLE001 - one source must never crash the run
                        logger.log(f"{source_name}: unexpected error for '{keyword}': {exc}")
                        failed += 1
                        continue
                    source_count += len(results)
                    raw_jobs.extend(results)
                status = " (some keywords blocked)" if blocked and source_count else ""
                logger.log(
                    f"{source_name}: {source_count} job(s) across {len(search_keywords)} keyword(s){status}."
                )

            logger.log(f"Total raw results before dedup: {len(raw_jobs)}.")
            new_jobs = _dedup_new_jobs(conn, raw_jobs)
            found = len(new_jobs)
            logger.log(f"{found} new job(s) after dedup.")

            if new_jobs:
                work_dir = config.data_dir / "llm-work"
                cv_text = get_cv_text(config)
                if not cv_text:
                    logger.log("No CV on file yet; filtering will run without CV context.")

                filter_results = llm_filter.filter_jobs(new_jobs, cv_text, work_dir=work_dir)

                queued: list[tuple[int, dict]] = []
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
                        failed += 1
                        continue
                    status_counts[result["status"]] = status_counts.get(result["status"], 0) + 1
                    if job_id is not None and result["status"] == "queued":
                        queued.append((company_id, job))

                filtered = status_counts.get("filtered_out", 0)
                logger.log(
                    "Filter results: "
                    f"{status_counts.get('queued', 0)} queued, "
                    f"{filtered} filtered out, "
                    f"{status_counts.get('new', 0)} left as new (filter failed)."
                )

                researched = 0
                seen_companies: set[int] = set()
                for company_id, job in queued:
                    if researched >= max_research:
                        logger.log(f"Reached the research cap ({max_research}) for this run.")
                        break
                    if company_id in seen_companies:
                        continue
                    seen_companies.add(company_id)
                    try:
                        company = db.get_company(conn, company_id)
                        if company is None or company["tldr"]:
                            continue
                        info = llm_research.research_company(
                            company["name"], job.get("title", ""), work_dir=work_dir
                        )
                        db.update_company_research(
                            conn,
                            company_id,
                            website=info["website"],
                            tldr=info["tldr"],
                            careers_url=info["careers_url"],
                        )
                    except Exception as exc:  # noqa: BLE001 - one bad company must never stop the rest
                        logger.log(f"Company research failed for company id {company_id}: {exc}")
                        failed += 1
                        continue
                    researched += 1
                    logger.log(f"Researched company: {company['name']}.")
                logger.log(f"Researched {researched} compan{'y' if researched == 1 else 'ies'} this run.")
            else:
                logger.log("Nothing new to filter or research.")

            auto_submit = get_auto_submit(conn)
            daily_cap = targets.get("daily_cap_per_source") or DEFAULT_DAILY_CAP_PER_SOURCE
            apply_applied, apply_failed = _run_apply_phase(
                conn, logger, daily_cap=daily_cap, auto_submit=auto_submit
            )
            applied += apply_applied
            failed += apply_failed
        except Exception as exc:  # noqa: BLE001 - the run must always finish and record something
            logger.log(f"Run failed unexpectedly: {exc}")
            failed += 1

        db.finish_run(conn, run_id, found=found, filtered=filtered, applied=applied, failed=failed, log=logger.text)
        return run_id
    finally:
        conn.close()


def _run_apply_phase(conn, logger: RunLogger, *, daily_cap: int, auto_submit: bool) -> tuple[int, int]:
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
    if not queued_jobs:
        return applied, failed

    logger.log(f"Apply phase: {len(queued_jobs)} job(s) queued, daily cap {daily_cap} per source.")
    for job in queued_jobs:
        source = job["source"]
        if source not in sent_today_by_source:
            sent_today_by_source[source] = db.count_applications_since(conn, source=source, since_iso=today_start)
        if sent_today_by_source[source] >= daily_cap:
            continue
        try:
            result = apply_to_job(job, auto_submit=auto_submit)
        except Exception as exc:  # noqa: BLE001 - one bad job must never stop the apply phase
            logger.log(f"Apply attempt crashed for job {job['id']} ({job['title']}): {exc}")
            failed += 1
            continue
        status = result.get("status")
        if status in APPLIED_STATUSES:
            applied += 1
            sent_today_by_source[source] = sent_today_by_source[source] + 1
            logger.log(f"Applied to job {job['id']} ({job['title']}) via {source}: {status}.")
        elif status == "needs_help":
            logger.log(f"Job {job['id']} ({job['title']}) needs help: {result.get('reason')}")
        else:
            failed += 1
            logger.log(f"Apply failed for job {job['id']} ({job['title']}): {result.get('reason')}")
    return applied, failed


if __name__ == "__main__":
    run_scan(trigger="scheduled")

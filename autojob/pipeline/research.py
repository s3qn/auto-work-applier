"""Re-research pass for companies whose direct-apply channels have not been
looked up yet: brand new companies from a scan just run, and existing
companies whose `tldr` predates the channel fields. Used by `run_scan` and
by the Jobs page's "Research companies for direct apply channels" button,
so a hands-off run keeps filling in `job_url_on_company_site`,
`general_cv_url`, and `cv_email` for companies already in the database.
"""

from __future__ import annotations

from pathlib import Path

from autojob import db
from autojob.llm import research as llm_research
from autojob.pipeline import events


def research_pending(
    conn,
    *,
    work_dir: Path,
    limit: int,
    run_id: int | None = None,
    logger=None,
) -> tuple[int, int]:
    """Research every queued job's company that has no `channels_researched_at`
    stamp yet, one job per company, newest job first, capped at `limit`.
    Returns (researched, failed). One bad company never stops the rest: the
    failure is logged and the pass moves on. A failed research call stores
    nothing (an existing tldr or careers URL stays as it was) and leaves the
    company unstamped, so the next pass tries it again.
    """
    jobs = db.list_queued_jobs_needing_research(conn, limit)
    if len(jobs) == limit:
        message = f"Reached the research cap ({limit}) for this run."
        if logger is not None:
            logger.log(message)
        if run_id is not None:
            events.emit(conn, run_id, "research", "info", message)

    researched = 0
    failed = 0
    total = len(jobs)
    for job in jobs:
        company_id = job["company_id"]
        company_name = job["company_name"]
        try:
            if run_id is not None:
                events.emit(conn, run_id, "research", "info", f"research: starting {company_name}.")
            info = llm_research.research_company(
                company_name, job["title"], work_dir=work_dir, job_url=job["url"]
            )
            if info.get("failed"):
                raise RuntimeError(info.get("tldr") or "research call failed")
            db.update_company_research(
                conn,
                company_id,
                website=info["website"],
                tldr=info["tldr"],
                careers_url=info["careers_url"],
                general_cv_url=info["general_cv_url"],
                cv_email=info["cv_email"],
                cv_email_source_url=info["cv_email_source_url"],
            )
            if info["job_url_on_company_site"]:
                db.set_job_company_site_url(conn, job["id"], info["job_url_on_company_site"])
        except Exception as exc:  # noqa: BLE001 - one bad company must never stop the rest
            message = f"Company research failed for company id {company_id}: {exc}"
            if logger is not None:
                logger.log(message)
            if run_id is not None:
                events.emit(conn, run_id, "research", "error", message)
            failed += 1
            continue
        researched += 1
        if logger is not None:
            logger.log(f"Researched company: {company_name}.")
        if run_id is not None:
            events.bump_progress(
                conn, run_id, "research", f"research: finished {company_name}.",
                done=researched, total=total,
            )
    if logger is not None:
        logger.log(f"Researched {researched} compan{'y' if researched == 1 else 'ies'} this run.")
    return researched, failed


def main() -> None:
    """`uv run python -m autojob.pipeline.research`: a one-off re-research
    pass outside a full scan run, using the same cap as a normal run."""
    from autojob.config import load_config
    from autojob.pipeline.run import MAX_RESEARCH_PER_RUN

    config = load_config()
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        work_dir = config.data_dir / "llm-work"
        count, failed = research_pending(conn, work_dir=work_dir, limit=MAX_RESEARCH_PER_RUN)
        print(f"Researched {count} compan{'y' if count == 1 else 'ies'}, {failed} failed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

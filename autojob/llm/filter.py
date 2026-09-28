"""Batch job filtering through the local claude CLI (haiku model)."""

from __future__ import annotations

from pathlib import Path

from autojob.llm.cli import ClaudeCliError, extract_json, run_claude

BATCH_SIZE = 10
FILTER_TIMEOUT = 180
FILTER_MODEL = "haiku"

FILTER_RULES = """Rules for keep vs drop:
- Keep offensive security, penetration testing, red team, security research, AppSec,
  SOC analyst, cyber security analyst, and security engineering roles asking for up to
  about 5 years of experience. Sean has about 2 years of practical offensive security
  experience, so roles somewhat above his level are fine to keep.
- Drop senior, lead, staff, principal, or manager security roles asking for 6+ years.
- Drop roles that require a security clearance Sean does not hold.
- Drop roles with no security or offensive security component at all.
- Drop roles located outside Israel unless the posting is explicitly remote."""


def _build_prompt(cv_text: str, jobs: list[dict]) -> str:
    blocks = []
    for i, job in enumerate(jobs):
        description = (job.get("description") or "").strip()[:2000] or "(no description available)"
        blocks.append(
            f"Job {i}:\n"
            f"Title: {job.get('title', '')}\n"
            f"Company: {job.get('company', '')}\n"
            f"Location: {job.get('location', '')}\n"
            f"Description: {description}"
        )
    jobs_text = "\n\n".join(blocks)
    cv_excerpt = cv_text.strip()[:6000] or "(no CV on file)"
    return (
        "You are screening job postings for Sean, a penetration tester, to decide which "
        "ones are worth applying to.\n\n"
        f"Sean's CV:\n{cv_excerpt}\n\n"
        f"{FILTER_RULES}\n\n"
        f"Jobs to evaluate:\n\n{jobs_text}\n\n"
        f"Respond with ONLY a JSON array of exactly {len(jobs)} objects, one per job in "
        'the same order: [{"fit_score": 0-10, "keep": true or false, "reason": "one short '
        'sentence"}, ...]. No prose, no markdown fences, just the JSON array.'
    )


def filter_jobs(jobs: list[dict], cv_text: str, *, work_dir: Path, batch_size: int = BATCH_SIZE) -> list[dict]:
    """Screen jobs against Sean's CV, several per CLI call.

    Returns one result dict per input job, same order:
    {"status": "queued"|"filtered_out"|"new", "fit_score": float|None, "reason": str}

    On any failure for a batch (timeout, bad JSON, wrong shape), every job in
    that batch gets status "new" and reason "filter failed" rather than
    crashing the run or silently dropping the job.
    """
    results: list[dict] = []
    for start in range(0, len(jobs), batch_size):
        batch = jobs[start : start + batch_size]
        try:
            prompt = _build_prompt(cv_text, batch)
            raw = run_claude(prompt, model=FILTER_MODEL, cwd=work_dir, timeout=FILTER_TIMEOUT)
            parsed = extract_json(raw)
            if not isinstance(parsed, list) or len(parsed) != len(batch):
                raise ClaudeCliError("filter response was not a JSON array matching the batch size")
            for item in parsed:
                if not isinstance(item, dict):
                    raise ClaudeCliError("filter response item was not a JSON object")
                fit_score = item.get("fit_score")
                results.append(
                    {
                        "status": "queued" if item.get("keep") else "filtered_out",
                        "fit_score": float(fit_score) if fit_score is not None else None,
                        "reason": str(item.get("reason") or "")[:300],
                    }
                )
        except (ClaudeCliError, ValueError, TypeError) as exc:
            reason = f"filter failed: {exc}"[:300]
            for _ in batch:
                results.append({"status": "new", "fit_score": None, "reason": reason})
    return results

"""Batch job filtering through the local claude CLI (haiku model)."""

from __future__ import annotations

import re
from pathlib import Path

from autojob.llm.cli import ClaudeCliError, extract_json, run_claude

BATCH_SIZE = 10
FILTER_TIMEOUT = 180
FILTER_MODEL = "haiku"

# Titles containing any of these are dropped before the LLM ever sees them. They
# are roles Sean, a junior penetration tester, has no path into: senior
# leadership, and whole non-technical professions that have nothing to do with
# security work. Matched as whole words, case-insensitive, against the title.
# Kept deliberately conservative so it never drops a real security role: for
# example "manager" and "director" are here, so a "Security Program Manager"
# is cut, which is intended (Sean wants hands-on roles, not management).
TITLE_BLOCKLIST = [
    # executive / management
    "cto", "ceo", "cfo", "coo", "ciso", "cmo", "vp", "vice president",
    "chief", "head of", "director", "manager", "team lead", "team leader",
    "founder", "partner", "board member",
    # non-technical professions
    "lawyer", "attorney", "legal counsel", "counsel", "paralegal", "advocate",
    "accountant", "bookkeeper", "controller", "auditor",
    "recruiter", "talent acquisition", "human resources", "sourcer",
    "sales", "account executive", "account manager", "business development",
    "marketing", "content writer", "copywriter", "social media",
    "designer", "ux", "ui/ux", "product manager", "project manager",
    "customer success", "customer support", "office manager", "receptionist",
    "recruitment", "finance", "procurement", "logistics",
    "physician", "nurse", "teacher", "recruiting",
]

_BLOCK_RE = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(t.strip()) for t in TITLE_BLOCKLIST) + r")(?![a-z])",
    re.IGNORECASE,
)


def title_is_blocked(title: str) -> str | None:
    """Return the blocklisted phrase if the title is an obvious non-fit, else None."""
    match = _BLOCK_RE.search(title or "")
    return match.group(1).strip() if match else None


FILTER_RULES = """Sean is a JUNIOR penetration tester: he holds HTB CPTS, TCM PNPT
and eJPT, has 100+ HackTheBox/TryHackMe machines and a home Active Directory lab,
and has limited professional experience (a switch-testing job, not real pentest
work). Treat him as early-career.

Decide keep vs drop:
- KEEP hands-on individual-contributor roles in offensive security, penetration
  testing, red team, security research, malware/vulnerability research, application
  security, SOC analyst, cyber security analyst, or security engineering. Roles
  somewhat above his level are fine; volume is wanted.
- DROP, no matter how well the words match, anything that is not a technical
  security or technical IT role Sean could actually do: management or executive
  roles (manager, lead, head, director, VP, chief, founder), and any non-technical
  profession (legal, sales, marketing, finance, HR, recruiting, design, product,
  support, operations).
- DROP roles requiring 6+ years or a senior/staff/principal title.
- DROP roles requiring a security clearance Sean does not hold.
- DROP roles located outside Israel unless the posting is explicitly remote.
Set keep=false whenever the role fails any DROP rule, even if fit_score is not low."""


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
    # First pass: cheap, deterministic title screen. Obvious non-fits (a lawyer,
    # a CTO) never reach the LLM, which saves calls and stops the model from
    # occasionally keeping them. Only the survivors go on to the LLM batch below.
    results: list[dict | None] = [None] * len(jobs)
    to_score: list[tuple[int, dict]] = []
    for idx, job in enumerate(jobs):
        blocked = title_is_blocked(job.get("title", ""))
        if blocked:
            results[idx] = {
                "status": "filtered_out",
                "fit_score": 0.0,
                "reason": f"title screen: not a hands-on security role ({blocked})",
            }
        else:
            to_score.append((idx, job))

    for start in range(0, len(to_score), batch_size):
        batch_pairs = to_score[start : start + batch_size]
        batch = [job for _, job in batch_pairs]
        batch_indices = [idx for idx, _ in batch_pairs]
        try:
            prompt = _build_prompt(cv_text, batch)
            raw = run_claude(prompt, model=FILTER_MODEL, cwd=work_dir, timeout=FILTER_TIMEOUT)
            parsed = extract_json(raw)
            if not isinstance(parsed, list) or len(parsed) != len(batch):
                raise ClaudeCliError("filter response was not a JSON array matching the batch size")
            for item, idx in zip(parsed, batch_indices):
                if not isinstance(item, dict):
                    raise ClaudeCliError("filter response item was not a JSON object")
                fit_score = item.get("fit_score")
                results[idx] = {
                    "status": "queued" if item.get("keep") else "filtered_out",
                    "fit_score": float(fit_score) if fit_score is not None else None,
                    "reason": str(item.get("reason") or "")[:300],
                }
        except (ClaudeCliError, ValueError, TypeError) as exc:
            reason = f"filter failed: {exc}"[:300]
            for idx in batch_indices:
                results[idx] = {"status": "new", "fit_score": None, "reason": reason}

    # Every slot is filled by the title screen or one of the branches above; the
    # fallback keeps the return type honest if a job somehow slipped through.
    return [r or {"status": "new", "fit_score": None, "reason": "not screened"} for r in results]

"""Per-company research through the local claude CLI (sonnet model, with
web search and web fetch allowed)."""

from __future__ import annotations

from pathlib import Path

from autojob.llm.cli import ClaudeCliError, extract_json, run_claude

RESEARCH_MODEL = "sonnet"
RESEARCH_TIMEOUT = 300
RESEARCH_ALLOWED_TOOLS = "WebSearch WebFetch"


def _build_prompt(company_name: str, job_title: str) -> str:
    return (
        f'Research the company "{company_name}", which is hiring for a "{job_title}" role. '
        "Sean is a penetration tester with about 2 years of practical offensive security "
        "experience (HTB CPTS, PNPT, eJPT certified), currently working as a Network "
        "Security Validation Engineer.\n\n"
        "Use web search to find real, current information about this company. Do not "
        'invent facts: if you cannot find something, say "unknown" for it.\n\n'
        "Respond with ONLY a JSON object:\n"
        '{"website": "https://... or null", '
        '"tldr": "2 to 4 plain sentences: what the company does, its size or stage if '
        'findable, what its security team does if findable, and why this job fits Sean", '
        '"careers_url": "https://... or null"}\n'
        "No prose, no markdown fences, just the JSON object."
    )


def research_company(company_name: str, job_title: str, *, work_dir: Path) -> dict:
    """Research one company. Returns {"website", "tldr", "careers_url"}.

    On any failure this returns a "unknown" tldr rather than raising, so a
    broken research call never crashes the run.
    """
    try:
        prompt = _build_prompt(company_name, job_title)
        raw = run_claude(
            prompt,
            model=RESEARCH_MODEL,
            cwd=work_dir,
            timeout=RESEARCH_TIMEOUT,
            allowed_tools=RESEARCH_ALLOWED_TOOLS,
        )
        parsed = extract_json(raw)
        if not isinstance(parsed, dict):
            raise ClaudeCliError("research response was not a JSON object")
        return {
            "website": parsed.get("website") or None,
            "tldr": str(parsed.get("tldr") or "unknown"),
            "careers_url": parsed.get("careers_url") or None,
        }
    except ClaudeCliError as exc:
        return {
            "website": None,
            "tldr": f"unknown (company research failed: {exc})"[:500],
            "careers_url": None,
        }

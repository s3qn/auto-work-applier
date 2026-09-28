"""Per-company research through the local claude CLI (sonnet model, with
web search and web fetch allowed)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from autojob.llm.cli import ClaudeCliError, extract_json, run_claude

RESEARCH_MODEL = "sonnet"
RESEARCH_TIMEOUT = 300
RESEARCH_ALLOWED_TOOLS = "WebSearch WebFetch"

SEAN_PROFILE = (
    "Sean is a junior penetration tester: HTB CPTS, TCM PNPT, and eJPT certified, "
    "100+ Hack The Box and TryHackMe machines solved, a home Active Directory lab, "
    "and close to no professional pentest experience."
)

# Hosts that are job boards or link shorteners, never a company's own site
# or ATS. A subdomain of any of these counts too.
JOB_BOARD_HOSTS = (
    "linkedin.com",
    "lnkd.in",
    "alljobs.co.il",
    "jobmaster.co.il",
    "drushim.co.il",
    "indeed.com",
    "indeed.co.il",
    "glassdoor.com",
    "jobnet.co.il",
    "gotfriends.co.il",
    "ziprecruiter.com",
    "monster.com",
)
# Boards that run a site per country (glassdoor.co.uk, indeed.de, ...):
# matched on the name label of the registrable domain, whatever the suffix.
JOB_BOARD_NAMES = ("glassdoor", "indeed", "linkedin", "monster")

# Second-level labels that sit under a country code TLD as a public suffix
# (acme.co.il, acme.com.au): the registrable domain then keeps three labels.
_SECOND_LEVEL_SUFFIXES = ("co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _host(value: str) -> str:
    host = (urlparse(value).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _registrable_domain(host: str) -> str:
    """Best-effort registrable domain without a public suffix list:
    `careers.acme.co.il` gives `acme.co.il`, `jobs.acme.com` gives
    `acme.com`."""
    labels = [label for label in host.lower().strip(".").split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _is_job_board_host(host: str) -> bool:
    if any(host == board or host.endswith("." + board) for board in JOB_BOARD_HOSTS):
        return True
    registrable = _registrable_domain(host)
    return registrable.split(".", 1)[0] in JOB_BOARD_NAMES


def _clean_url(value) -> str | None:
    """Keep only http(s) URLs whose host is not a job board. Anything else
    (a non-string, a relative path, a job board link) is dropped."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if _is_job_board_host(_host(value)):
        return None
    return value


def _clean_email(email, source_url, website=None) -> tuple[str | None, str | None]:
    """An email is only trusted when it comes with a real source page it was
    seen on, and its domain belongs to the company: the same registrable
    domain as the company website or as the source page (subdomains on
    either side are fine). Returns (None, None) otherwise, including for an
    address on a job board's own domain."""
    clean_source = _clean_url(source_url)
    if clean_source is None:
        return None, None
    if not isinstance(email, str):
        return None, None
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        return None, None
    email_domain = email.rsplit("@", 1)[1]
    if _is_job_board_host(email_domain):
        return None, None
    allowed = {_registrable_domain(_host(clean_source))}
    clean_website = _clean_url(website)
    if clean_website:
        allowed.add(_registrable_domain(_host(clean_website)))
    allowed.discard("")
    if _registrable_domain(email_domain) not in allowed:
        return None, None
    return email, clean_source


def _build_prompt(company_name: str, job_title: str, job_url: str = "") -> str:
    posting_line = f" The posting Sean is looking at is {job_url}." if job_url else ""
    return (
        f'Research the company "{company_name}", which is hiring for a "{job_title}" role.'
        f"{posting_line}\n\n"
        f"{SEAN_PROFILE}\n\n"
        "Use web search and web fetch to find real, current information about this company. "
        "Do not invent facts: if you cannot find something, use null for it.\n\n"
        "Rules for every URL and email you return:\n"
        "- Every URL must be a page you actually fetched with WebFetch, never a guess.\n"
        "- job_url_on_company_site is this posting on the company's own site, or on its own "
        "ATS (Comeet, Greenhouse, Lever, or similar) linked from the company site. Never a "
        "job board (LinkedIn, AllJobs, JobMaster, Drushim, Indeed, Glassdoor).\n"
        "- cv_email must be an email address literally seen on a page you fetched, with that "
        "exact page as cv_email_source_url. Never guess an address from a pattern like "
        "jobs@<domain>.\n\n"
        "Respond with ONLY a JSON object with exactly these keys:\n"
        '{"website": "https://... or null", '
        '"tldr": "2 to 4 plain sentences: what the company does, its size or stage if '
        'findable, what its security team does if findable, and why this job fits Sean", '
        '"careers_url": "https://... or null", '
        '"job_url_on_company_site": "https://... or null", '
        '"general_cv_url": "https://... or null (a general send-us-your-CV page)", '
        '"cv_email": "email or null", '
        '"cv_email_source_url": "https://... or null"}\n'
        "No prose, no markdown fences, just the JSON object."
    )


def research_company(company_name: str, job_title: str, *, work_dir: Path, job_url: str = "") -> dict:
    """Research one company. Returns a dict with website, tldr, careers_url,
    job_url_on_company_site, general_cv_url, cv_email, cv_email_source_url,
    and `failed`.

    On any failure this returns `failed: True`, an "unknown" tldr and None
    for every other field, rather than raising, so a broken research call
    never crashes the run. Callers must not store a failed result over
    what they already know.
    """
    try:
        prompt = _build_prompt(company_name, job_title, job_url)
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
        website = _clean_url(parsed.get("website"))
        cv_email, cv_email_source_url = _clean_email(
            parsed.get("cv_email"), parsed.get("cv_email_source_url"), website
        )
        return {
            "website": website,
            "tldr": str(parsed.get("tldr") or "unknown"),
            "careers_url": _clean_url(parsed.get("careers_url")),
            "job_url_on_company_site": _clean_url(parsed.get("job_url_on_company_site")),
            "general_cv_url": _clean_url(parsed.get("general_cv_url")),
            "cv_email": cv_email,
            "cv_email_source_url": cv_email_source_url,
            "failed": False,
        }
    except ClaudeCliError as exc:
        return {
            "website": None,
            "tldr": f"unknown (company research failed: {exc})"[:500],
            "careers_url": None,
            "job_url_on_company_site": None,
            "general_cv_url": None,
            "cv_email": None,
            "cv_email_source_url": None,
            "failed": True,
        }

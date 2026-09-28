"""Shared field-mapping helpers for the ATS handlers.

`match_answer` turns a form field's visible label into an honest answer from
the merged answer set (profile facts, the recurring-question answer bank,
and the per-job generated cover letter). It is deliberately conservative:
it only maps labels that clearly correspond to a fact Sean actually has an
answer for. A tool-specific proficiency question ("years of experience with
PostgreSQL?"), a yes/no eligibility question, or anything else it doesn't
recognize returns None, so the handler can leave it blank (optional) or
escalate it to a help item (required) instead of guessing.
"""

from __future__ import annotations

import re

# Named technologies in a "years of experience" style question mean the
# question is about that specific tool, not Sean's general pentest
# experience, so the answer bank's years_experience entry does not apply.
TECH_NAME_HINTS = (
    "python", "java", "c#", "c++", "sql", "postgres", "mysql", "aws", "azure",
    "gcp", "linux", "docker", "kubernetes", "react", "angular", "vue",
    "javascript", "typescript", "node", "ruby", "php", "golang", "rust",
    "salesforce", "terraform", "ansible", "burp", "nmap", "metasploit",
)


def _norm(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "")).strip().lower()


def is_required_label(label: str) -> bool:
    """Greenhouse marks a required field with a trailing "*"; Lever with a
    "required" class and a "&#10038;" glyph rendered as text. Check for
    both, and for the Hebrew "חובה" (mandatory) some company career pages
    use, rather than relying on the HTML5 `required` attribute, which these
    ATSes don't consistently set on their own custom question inputs."""
    text = label or ""
    return "*" in text or "✱" in text or "required" in text.lower() or "חובה" in text


def match_answer(label: str, answers: dict) -> str | None:
    """Return an honest answer string for this label, or None if this isn't
    a field the answer set can confidently fill in."""
    text = _norm(label)
    if not text:
        return None

    if "first name" in text:
        full = (answers.get("full_name") or "").strip()
        return full.split()[0] if full else None
    if "last name" in text:
        full = (answers.get("full_name") or "").strip()
        parts = full.split()
        return " ".join(parts[1:]) if len(parts) > 1 else None
    if text in ("name", "full name", "your name", "candidate name"):
        return answers.get("full_name") or None
    if "email" in text:
        return answers.get("email") or None
    if "phone" in text:
        return answers.get("phone") or None
    if any(k in text for k in ("linkedin",)):
        return answers.get("linkedin_url") or None
    if any(k in text for k in ("github",)):
        return answers.get("github_url") or None
    if any(k in text for k in ("hackthebox", "hack the box", "htb")):
        return answers.get("htb_profile_url") or None
    if any(k in text for k in ("portfolio", "website", "personal site", "other url", "profile url")):
        return answers.get("linkedin_url") or answers.get("github_url") or answers.get("htb_profile_url") or None
    if any(k in text for k in ("city", "location", "where do you live", "where are you based", "based in")):
        return answers.get("city") or None
    if "country" in text:
        return answers.get("country") or None
    if "certificat" in text:
        return answers.get("certifications") or None
    if any(k in text for k in ("visa", "sponsorship")):
        return answers.get("visa_sponsorship") or None
    if "relocat" in text:
        return answers.get("relocation") or None
    if any(k in text for k in ("salary", "compensation", "expected pay", "pay expectation")):
        return answers.get("salary_expectation") or None
    if any(k in text for k in ("notice period", "availability", "start date", "when can you start")):
        return answers.get("availability_notice") or None
    if "driving" in text or "licence" in text or "license" in text:
        return answers.get("driving_licence") or None
    if "cover letter" in text:
        return answers.get("cover_letter") or None
    if any(k in text for k in ("how did you hear", "how did you find", "referr")):
        return "Found through an automated job search."
    if "years" in text and "experience" in text:
        if any(hint in text for hint in TECH_NAME_HINTS):
            return None  # tool-specific: no honest basis to answer generically
        if any(k in text for k in ("penetration", "pentest", "security", "cyber", "offensive")):
            return answers.get("years_experience") or None
        if any(k in text for k in ("professional", "full-time", "full time", "total")):
            return answers.get("years_experience") or None
        return None
    return None

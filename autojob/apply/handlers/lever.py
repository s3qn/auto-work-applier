"""Deterministic apply handler for Lever's public job application forms
(`jobs.lever.co`).

Confirmed live on 2026-09-28: a Lever job posting page only links to the
actual form, which lives at `<job url>/apply`. That form has stable field
names `name`, `email`, `phone`, `location`, `urls[LinkedIn]`,
`urls[GitHub]`, `urls[Portfolio]`, and a file input named `resume`. Custom
questions render as `<li class="application-question">` with a
`.application-label` (the question text, required ones marked with a
"required" class and a star-like glyph) and an `.application-field`
containing the input. Multi-choice questions (radio/checkbox/select) are not
mapped here: there is no generically safe way to guess which option is
honest across different companies' wording, so a required one always
escalates. The submit control is `button#btn-submit` (a `type="button"`
with a click handler, not a native form submit).
"""

from __future__ import annotations

from pathlib import Path

from autojob.apply.fields import is_required_label, match_answer

DOMAIN_MARKER = "jobs.lever.co"

# Lever also wraps the standard fields (name, email, phone, location, the
# resume upload, and the profile URLs) in their own `li.application-question`
# elements alongside the real custom questions. Those are already filled by
# the explicit calls above the loop below, so the loop must recognize and
# skip them by field name rather than re-processing (and potentially
# false-flagging) them as unanswerable custom questions.
HANDLED_FIELD_NAMES = {
    "name", "email", "phone", "location", "resume",
    "urls[LinkedIn]", "urls[GitHub]", "urls[Portfolio]",
}
TEXT_LIKE_SELECTOR = 'input[type="text"], input[type="email"], input[type="tel"], input[type="url"], textarea'


def matches(url: str) -> bool:
    return DOMAIN_MARKER in (url or "").lower()


def application_url(url: str) -> str:
    trimmed = (url or "").rstrip("/")
    return trimmed if trimmed.endswith("/apply") else f"{trimmed}/apply"


def _fill(page, selector: str, value: str | None) -> None:
    if not value:
        return
    el = page.query_selector(selector)
    if el is not None:
        el.fill(value)


def apply(page, answers: dict, cv_path: Path | None, auto_submit: bool) -> dict:
    current_url = page.url or ""
    if not current_url.rstrip("/").endswith("/apply"):
        page.goto(application_url(current_url))

    _fill(page, 'input[name="name"]', answers.get("full_name"))
    _fill(page, 'input[name="email"]', answers.get("email"))
    _fill(page, 'input[name="phone"]', answers.get("phone"))
    _fill(page, 'input[name="location"], #location-input', answers.get("city"))
    _fill(page, 'input[name="urls[LinkedIn]"]', answers.get("linkedin_url"))
    _fill(page, 'input[name="urls[GitHub]"]', answers.get("github_url"))
    _fill(page, 'input[name="urls[Portfolio]"]', answers.get("htb_profile_url"))

    resume_input = page.query_selector('input[name="resume"], #resume-upload-input')
    if resume_input is not None and cv_path is not None:
        resume_input.set_input_files(str(cv_path))

    cover_letter_field = page.query_selector('textarea[name="comments"], textarea[name="cover_letter"]')
    if cover_letter_field is not None and answers.get("cover_letter"):
        cover_letter_field.fill(answers["cover_letter"])

    unmapped_required: list[str] = []
    for question in page.query_selector_all("li.application-question"):
        field_area = question.query_selector(".application-field")
        if field_area is None:
            continue
        any_field = field_area.query_selector("input, textarea, select")
        field_name = any_field.get_attribute("name") if any_field is not None else None
        if field_name in HANDLED_FIELD_NAMES:
            continue
        label_el = question.query_selector(".application-label")
        label = (label_el.text_content() or "").strip() if label_el else ""
        required = is_required_label(label) or question.query_selector(".required") is not None
        text_field = field_area.query_selector(TEXT_LIKE_SELECTOR)
        if text_field is None:
            # radio/checkbox/select: no generically safe way to guess.
            if required:
                unmapped_required.append(label or "unlabeled question")
            continue
        answer = match_answer(label, answers)
        if answer:
            text_field.fill(answer)
        elif required:
            unmapped_required.append(label or "unlabeled question")

    if unmapped_required:
        return {
            "status": "needs_help",
            "reason": "Lever form has required question(s) with no honest answer on file: "
            + "; ".join(unmapped_required[:5]),
        }

    if not auto_submit:
        return {"status": "ready_to_submit", "reason": None}

    submit_button = page.query_selector('#btn-submit, [data-qa="btn-submit"]')
    if submit_button is None:
        return {"status": "needs_help", "reason": "Could not find the Lever submit button."}
    submit_button.click()
    return {"status": "sent", "reason": None}

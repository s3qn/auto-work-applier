"""Deterministic apply handler for Greenhouse's public job board
application forms (`boards.greenhouse.io` and `job-boards.greenhouse.io`).

Confirmed live on 2026-09-28 against a real Greenhouse-hosted posting: the
form renders on the job page itself (no separate apply page) with stable
field ids `#first_name`, `#last_name`, `#email`, `#phone`,
`#candidate-location`, `#country`, a file input `#resume`, and any custom
questions as `input[id^="question_..."]` or `textarea[id^="question_..."]`,
each tied to a `<label for="...">`. The submit button is
`button[type="submit"]`.
"""

from __future__ import annotations

from pathlib import Path

from autojob.apply.fields import is_required_label, match_answer

DOMAIN_MARKERS = ("boards.greenhouse.io", "job-boards.greenhouse.io")


def matches(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in DOMAIN_MARKERS)


def _fill(page, selector: str, value: str | None) -> None:
    if not value:
        return
    el = page.query_selector(selector)
    if el is not None:
        el.fill(value)


def _label_text(page, field) -> str:
    field_id = field.get_attribute("id")
    if not field_id:
        return ""
    label_el = page.query_selector(f'label[for="{field_id}"]')
    return (label_el.text_content() or "").strip() if label_el else ""


def apply(page, answers: dict, cv_path: Path | None, auto_submit: bool) -> dict:
    full_name_parts = (answers.get("full_name") or "").split()
    _fill(page, "#first_name", full_name_parts[0] if full_name_parts else None)
    _fill(page, "#last_name", " ".join(full_name_parts[1:]) if len(full_name_parts) > 1 else None)
    _fill(page, "#email", answers.get("email"))
    _fill(page, "#phone", answers.get("phone"))
    _fill(page, "#candidate-location", answers.get("city"))
    _fill(page, "#country", answers.get("country"))

    resume_input = page.query_selector("#resume") or page.query_selector('input[type="file"]')
    if resume_input is not None and cv_path is not None:
        resume_input.set_input_files(str(cv_path))

    cover_letter_field = page.query_selector("#cover_letter_text")
    if cover_letter_field is not None and answers.get("cover_letter"):
        cover_letter_field.fill(answers["cover_letter"])

    unmapped_required: list[str] = []
    for field in page.query_selector_all('input[id^="question_"], textarea[id^="question_"]'):
        if (field.get_attribute("type") or "") == "file":
            continue
        label = _label_text(page, field)
        answer = match_answer(label, answers)
        if answer:
            field.fill(answer)
        elif is_required_label(label):
            unmapped_required.append(label or "unlabeled question")

    if unmapped_required:
        return {
            "status": "needs_help",
            "reason": "Greenhouse form has required question(s) with no honest answer on file: "
            + "; ".join(unmapped_required[:5]),
        }

    if not auto_submit:
        return {"status": "ready_to_submit", "reason": None}

    submit_button = page.query_selector('button[type="submit"]')
    if submit_button is None:
        return {"status": "needs_help", "reason": "Could not find the Greenhouse submit button."}
    submit_button.click()
    return {"status": "sent", "reason": None}

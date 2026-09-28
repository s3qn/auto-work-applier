"""Deterministic apply handler for Comeet's public job application forms.

A Comeet posting on `comeet.com` (or embedded on a company's own careers
page) does not put the form on the listing page itself: clicking its
"Apply for this job" button injects an iframe served from `comeet.co` (a
different TLD from the listing, not a typo) at a `/apply` path. Confirmed
live on 2026-09-28 against a real posting: inside that iframe the fields are
`#inputFirstName`, `#inputLastName`, `#inputEmail`, `#inputTel`,
`#inputLink` (a single generic profile URL field, not separate LinkedIn/
GitHub fields), a file input `#cv`, and, unusually, a FILE input for the
cover letter (`#coverLetter`) rather than a text box: Comeet wants an
attached document, not typed text. This handler writes the generated cover
letter out to `answers["cover_letter_file"]` (a path the caller is expected
to set before calling `apply`, see `autojob.apply.apply`) and uploads that.
The submit control is `button.applyButton`.

Per-company custom questions were not present on the posting used to build
this handler, so the custom-question loop below (matched on a generic
`.question`/`.customQuestion` wrapper) is a best-effort guess, not something
confirmed live. A required custom question this handler can't recognize
still escalates safely to `needs_help` rather than being silently skipped.
"""

from __future__ import annotations

from pathlib import Path

from autojob.apply.fields import is_required_label, match_answer

DOMAIN_MARKERS = ("comeet.com", "comeet.co")
APPLY_BUTTON_TEXT = "Apply for this job"
FRAME_URL_MARKER = "comeet.co"


def matches(url: str) -> bool:
    return any(marker in (url or "").lower() for marker in DOMAIN_MARKERS)


def matches_page(page) -> bool:
    """Fallback for a Comeet widget embedded on a company's own domain with
    no URL marker to go on: look for the iframe it always injects."""
    try:
        return any(FRAME_URL_MARKER in (frame.url or "") for frame in page.frames)
    except Exception:  # noqa: BLE001 - best-effort detection only
        return False


def _find_apply_frame(page):
    for frame in page.frames:
        url = frame.url or ""
        if FRAME_URL_MARKER in url and "/apply" in url:
            return frame
    return None


def _fill(target, selector: str, value: str | None) -> None:
    if not value:
        return
    el = target.query_selector(selector)
    if el is not None:
        el.fill(value)


def apply(page, answers: dict, cv_path: Path | None, auto_submit: bool) -> dict:
    frame = _find_apply_frame(page)
    if frame is None:
        apply_button = page.query_selector(f'button:has-text("{APPLY_BUTTON_TEXT}")')
        if apply_button is not None:
            apply_button.click()
            page.wait_for_timeout(2000)
        frame = _find_apply_frame(page)
    if frame is None:
        return {"status": "needs_help", "reason": "Could not find the Comeet application form."}

    full_name_parts = (answers.get("full_name") or "").split()
    _fill(frame, "#inputFirstName", full_name_parts[0] if full_name_parts else None)
    _fill(frame, "#inputLastName", " ".join(full_name_parts[1:]) if len(full_name_parts) > 1 else None)
    _fill(frame, "#inputEmail", answers.get("email"))
    _fill(frame, "#inputTel", answers.get("phone"))
    _fill(
        frame,
        "#inputLink",
        answers.get("linkedin_url") or answers.get("github_url") or answers.get("htb_profile_url"),
    )
    _fill(frame, "#inputNote", answers.get("cover_letter"))

    resume_input = frame.query_selector("#cv")
    if resume_input is not None and cv_path is not None:
        resume_input.set_input_files(str(cv_path))

    cover_letter_field = frame.query_selector("#coverLetter")
    cover_letter_file = answers.get("cover_letter_file")
    if cover_letter_field is not None and cover_letter_file:
        cover_letter_field.set_input_files(str(cover_letter_file))

    unmapped_required: list[str] = []
    for question in frame.query_selector_all(".customQuestion, .question"):
        text_field = question.query_selector('input[type="text"], textarea')
        label_el = question.query_selector("label")
        label = (label_el.text_content() or "").strip() if label_el else ""
        if text_field is None:
            if is_required_label(label):
                unmapped_required.append(label or "unlabeled question")
            continue
        answer = match_answer(label, answers)
        if answer:
            text_field.fill(answer)
        elif is_required_label(label):
            unmapped_required.append(label or "unlabeled question")

    if unmapped_required:
        return {
            "status": "needs_help",
            "reason": "Comeet form has required question(s) with no honest answer on file: "
            + "; ".join(unmapped_required[:5]),
        }

    if not auto_submit:
        return {"status": "ready_to_submit", "reason": None}

    submit_button = frame.query_selector("button.applyButton") or frame.query_selector('[ng-click="apply()"]')
    if submit_button is None:
        return {"status": "needs_help", "reason": "Could not find the Comeet submit button."}
    submit_button.click()
    return {"status": "sent", "reason": None}

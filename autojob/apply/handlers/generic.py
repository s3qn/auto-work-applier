"""Generic browser-plus-LLM apply handler: the fallback for every job that
is not on Greenhouse, Lever, or Comeet (the company career pages this build
was built for, such as catonetworks.com/careers, akamai.com/careers,
top-soft.co.il/jobs, malam, matrix and lema.ai, plus AllJobs' logged-in
"send CV" flow and LinkedIn listings).

Where the ATS-specific handlers are deterministic (fixed selectors
confirmed against one real posting), this one is necessarily heuristic: it
finds an apply affordance, follows it to whatever renders the form (same
page, a modal, an injected iframe, a new tab, or an external ATS domain),
harvests every visible field with its label, asks the local claude CLI to
map each field to an honest answer, a skip, or an escalation, and fills in
what it can. A field is only ever filled from Sean's honest profile
answers or the model's tailored, honest text; anything requiring a claim he
cannot honestly make escalates to `needs_help` instead of guessing, exactly
like the ATS-specific handlers.

Same 4-argument contract as `handlers/greenhouse.py`, `handlers/lever.py`
and `handlers/comeet.py` (`apply(page, answers, cv_path, auto_submit)`), so
`apply.py`'s finaliser (screenshot capture, help item creation, status
update) works unchanged for this handler too. `apply.py` enriches the
`answers` dict with `_job_title`, `_company_name` and `_company_tldr` before
calling any handler so this one has job context for the LLM prompt; the
ATS-specific handlers simply ignore those extra keys.

If reaching the form lands on a domain one of the specific handlers already
covers (a company's own "Apply" button often just redirects to a
Greenhouse, Lever, or Comeet-hosted form), this hands off to that handler
instead of harvesting generically.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from autojob.apply.answers import HONESTY_INSTRUCTIONS
from autojob.apply.fields import is_required_label, match_answer
from autojob.config import load_config
from autojob.llm.cli import ClaudeCliError, extract_json, run_claude

MAPPING_MODEL = "sonnet"
MAPPING_TIMEOUT = 180
NAV_WAIT_MS = 2000
SETTLE_TIMEOUT_MS = 5000
MAX_HARVEST_FIELDS = 60
FORM_FIELD_THRESHOLD = 3
# Footer newsletter boxes, site search and nav widgets are never an
# application form: harvest skips any field inside one of these.
EXCLUDED_CONTAINER_JS = (
    "el => !!el.closest('footer, nav, header, aside, [role=search], form[role=search], "
    "form[action*=search i], form[action*=subscribe i], form[action*=newsletter i], "
    "form[id*=newsletter i], form[class*=newsletter i], form[class*=subscribe i]')"
)
EASY_APPLY_ONLY_REASON = (
    "LinkedIn listing only offers Easy Apply, which this build never automates. Apply by hand: {url}"
)

ALLJOBS_UPLOAD_MARKER = "alljobs.co.il/search/uploadsingle"
LINKEDIN_JOB_MARKER = "linkedin.com/jobs/view"

APPLY_TEXT_PATTERNS = (
    "apply now",
    "apply for this job",
    "apply for this position",
    "submit application",
    "submit your application",
    "apply",
    "הגש מועמדות",
    "הגישו מועמדות",
    "הגשת מועמדות",
    "שלח קורות חיים",
    "שליחת קורות חיים",
    "לפרטים והגשה",
)
LOGIN_TEXT_MARKERS = (
    "sign in", "log in", "login", "התחבר", "התחברות", "כניסה לאתר", "הרשמה",
)
CAPTCHA_URL_MARKERS = ("recaptcha", "hcaptcha", "captcha", "turnstile")
SUBMIT_TEXT_PATTERNS = ("submit", "send application", "שלח", "הגש")
COVER_LETTER_LABEL_MARKERS = ("cover letter", "motivation", "מכתב מקדים", "מכתב פנייה")

INPUT_SELECTOR = (
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"])'
    ':not([type="image"]):not([type="reset"])'
)
CLICKABLE_SELECTOR = 'a, button, [role="button"], input[type="submit"]'

MAPPING_INSTRUCTIONS = (
    "You are filling a real job application form for Sean, a junior penetration "
    "tester. You are given the form's fields (a key, its label, its type, whether "
    "it is required, and its options for a select or radio group) and Sean's "
    "honest answer set. For every field, respond with exactly one of:\n"
    '  {"key": "<field key>", "value": "<the honest text to type, or the exact '
    'option text to select>"}\n'
    '  {"key": "<field key>", "skip": true}\n'
    '  {"key": "<field key>", "escalate": "<one short sentence: why Sean must '
    'answer this himself>"}\n'
    "Use skip for an optional field with no honest answer available. Use escalate "
    "for a required field asking for a claim Sean cannot honestly make, a yes/no "
    "eligibility question with no basis in his profile, or any value the answer "
    "set does not contain. For a select or radio field, value must be one of the "
    "exact option strings given, never a fabricated option. Never invent "
    "experience, employers, achievements, or credentials."
)


def matches(url: str) -> bool:
    """Every non-empty URL "matches" the generic handler: it is the
    fallback `router.pick_handler` reaches for once greenhouse, lever and
    comeet have had a chance, not part of the ordered `HANDLERS` list those
    three are matched against."""
    return bool((url or "").strip())


def apply(page, answers: dict, cv_path: Path | None, auto_submit: bool) -> dict:
    config = load_config()
    work_dir = config.data_dir / "llm-work"
    job_context = {
        "title": answers.get("_job_title", ""),
        "company": answers.get("_company_name", ""),
        "company_tldr": answers.get("_company_tldr", ""),
    }
    try:
        url = (_current_url(page) or "").lower()
        if ALLJOBS_UPLOAD_MARKER in url:
            return _handle_alljobs(page, answers, cv_path, auto_submit, job_context, work_dir)
        if LINKEDIN_JOB_MARKER in url:
            return _handle_linkedin(page, answers, cv_path, auto_submit, job_context, work_dir)

        scope = _reach_application_form(page)
        if scope is None:
            return {"status": "needs_help", "reason": "Could not locate an application form on this page."}

        delegated = _delegate_if_known_ats(page, answers, cv_path, auto_submit)
        if delegated is not None:
            return delegated

        return _harvest_map_fill(scope, answers, cv_path, auto_submit, job_context, work_dir)
    except Exception as exc:  # noqa: BLE001 - one bad page must never crash the apply run
        return {"status": "needs_help", "reason": f"Apply attempt hit an unexpected error: {exc}"}


def _delegate_if_known_ats(page, answers, cv_path, auto_submit):
    """After following whatever affordance reached the form, the page (or
    its embedded widget) may now be one the deterministic handlers already
    cover. Imports `router` lazily: `router.py` imports this module, so a
    module-level import here would be circular."""
    from autojob.apply import router

    known = router.pick_handler_by_url(_current_url(page))
    if known is not None:
        return known.apply(page, answers, cv_path, auto_submit)
    if router.comeet.matches_page(page):
        return router.comeet.apply(page, answers, cv_path, auto_submit)
    return None


# --- reaching the form ------------------------------------------------


def _reach_application_form(page):
    """Return the scope (the page itself, an injected iframe, or a new tab)
    that holds the application form, clicking an apply affordance first if
    the page currently looks like a listing rather than a form. Returns
    None only when nothing that looks like a form could be found at all."""
    if _looks_like_form(page):
        return page
    affordance = find_apply_affordance(page)
    if affordance is None:
        return page if _looks_like_form(page) else None
    try:
        affordance.click()
    except Exception:
        return None
    _settle(page)
    if _looks_like_form(page):
        return page
    frame = _find_form_frame(page)
    if frame is not None:
        return frame
    popup = _newest_popup(page)
    if popup is not None:
        _settle(popup)
        if _looks_like_form(popup):
            return popup
    return page if _looks_like_form(page) else None


def find_apply_affordance(scope):
    """The first visible clickable element whose text or aria-label looks
    like an apply call to action, in English or Hebrew, or None."""
    return _find_button_by_text(scope, APPLY_TEXT_PATTERNS)


def _find_button_by_text(scope, patterns, exclude=()):
    try:
        candidates = scope.query_selector_all(CLICKABLE_SELECTOR)
    except Exception:
        return None
    for el in candidates:
        if not _visible(el):
            continue
        text = _text_content(el).lower()
        aria = _safe_attr(el, "aria-label").lower()
        combined = f"{text} {aria}"
        if any(pattern in combined for pattern in exclude):
            continue
        if any(pattern in combined for pattern in patterns):
            return el
    return None


def _looks_like_form(scope) -> bool:
    """At least three fields, or two when one of them is a file input (a
    name plus a CV upload is a real, if short, application form)."""
    fields, _ = harvest_fields(scope)
    if len(fields) >= FORM_FIELD_THRESHOLD:
        return True
    return len(fields) >= 2 and any(f.get("type") == "file" for f in fields)


def _in_excluded_container(el) -> bool:
    evaluate = getattr(el, "evaluate", None)
    if not callable(evaluate):
        return False
    try:
        return bool(evaluate(EXCLUDED_CONTAINER_JS))
    except Exception:
        return False


def _find_form_frame(page):
    try:
        frames = list(page.frames)
    except Exception:
        return None
    main_frame = getattr(page, "main_frame", None)
    for frame in frames:
        if frame is main_frame or frame is page:
            continue
        if _looks_like_form(frame):
            return frame
    return None


def _newest_popup(page):
    try:
        pages = page.context.pages
    except Exception:
        return None
    if len(pages) <= 1:
        return None
    newest = pages[-1]
    return newest if newest is not page else None


def _settle(scope) -> None:
    wait_for_load_state = getattr(scope, "wait_for_load_state", None)
    if callable(wait_for_load_state):
        try:
            wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
            return
        except Exception:
            pass
    wait_for_timeout = getattr(scope, "wait_for_timeout", None)
    if callable(wait_for_timeout):
        try:
            wait_for_timeout(NAV_WAIT_MS)
        except Exception:
            pass


# --- harvesting ---------------------------------------------------------


def harvest_fields(scope) -> tuple[list[dict], dict]:
    """Collect every visible input, textarea, select and radio/checkbox
    group on `scope` (a Playwright page or frame), each with its label.

    Returns `(fields, elements)`: `fields` is a compact, JSON-safe list
    (one dict per field: key, label, type, required, and options for a
    select or radio/checkbox group) ready to hand to the LLM, and
    `elements` maps each field's key to the live element handle (or, for a
    radio/checkbox group, a list of `(option_label, element)` pairs) used
    to fill it afterwards.
    """
    fields: list[dict] = []
    elements: dict = {}
    groups: dict[str, str] = {}  # field name -> the group's field key
    index = 0

    def next_key() -> str:
        nonlocal index
        key = f"f{index}"
        index += 1
        return key

    try:
        inputs = scope.query_selector_all(INPUT_SELECTOR)
    except Exception:
        inputs = []
    for el in inputs:
        if len(fields) >= MAX_HARVEST_FIELDS:
            return fields, elements
        if not _visible(el) or _in_excluded_container(el):
            continue
        input_type = (_safe_attr(el, "type") or "text").lower()
        name = _safe_attr(el, "name")
        if input_type in ("radio", "checkbox") and name:
            option_label = _label_for(scope, el)
            group_key = groups.get(name)
            if group_key is None:
                group_key = next_key()
                groups[name] = group_key
                descriptor = {
                    "key": group_key, "label": option_label or name, "type": input_type,
                    "required": _looks_required(el, option_label), "options": [],
                }
                fields.append(descriptor)
                elements[group_key] = []
            else:
                descriptor = next(f for f in fields if f["key"] == group_key)
            descriptor["options"].append(option_label or name)
            elements[group_key].append((option_label or name, el))
            continue
        label = _label_for(scope, el)
        key = next_key()
        fields.append({
            "key": key, "label": label, "type": input_type,
            "required": _looks_required(el, label),
        })
        elements[key] = el

    if len(fields) < MAX_HARVEST_FIELDS:
        try:
            textareas = scope.query_selector_all("textarea")
        except Exception:
            textareas = []
        for el in textareas:
            if len(fields) >= MAX_HARVEST_FIELDS:
                break
            if not _visible(el) or _in_excluded_container(el):
                continue
            label = _label_for(scope, el)
            key = next_key()
            fields.append({
                "key": key, "label": label, "type": "textarea",
                "required": _looks_required(el, label),
            })
            elements[key] = el

    if len(fields) < MAX_HARVEST_FIELDS:
        try:
            selects = scope.query_selector_all("select")
        except Exception:
            selects = []
        for el in selects:
            if len(fields) >= MAX_HARVEST_FIELDS:
                break
            if not _visible(el) or _in_excluded_container(el):
                continue
            label = _label_for(scope, el)
            key = next_key()
            fields.append({
                "key": key, "label": label, "type": "select",
                "required": _looks_required(el, label), "options": _select_options(el),
            })
            elements[key] = el

    return fields, elements


def _select_options(el) -> list[str]:
    try:
        options = el.query_selector_all("option")
    except Exception:
        return []
    return [text for text in (_text_content(opt) for opt in options) if text]


def _label_for(scope, el) -> str:
    field_id = _safe_attr(el, "id")
    if field_id:
        try:
            label_el = scope.query_selector(f'label[for="{field_id}"]')
        except Exception:
            label_el = None
        if label_el is not None:
            text = _text_content(label_el)
            if text:
                return text
    for attr in ("aria-label", "placeholder"):
        value = _safe_attr(el, attr)
        if value:
            return value
    # A field the page nests directly inside its own <label> instead of
    # linking with a `for` attribute. Best-effort: real Playwright elements
    # support `.evaluate()`; test fakes generally do not, and simply skip
    # this step.
    evaluate = getattr(el, "evaluate", None)
    if callable(evaluate):
        try:
            wrapper_text = evaluate("el => { const l = el.closest('label'); return l ? l.textContent : ''; }")
            if wrapper_text:
                return re.sub(r"\s+", " ", wrapper_text).strip()
        except Exception:
            pass
    return ""


def _looks_required(el, label: str) -> bool:
    if is_required_label(label):
        return True
    if _has_attr(el, "required"):
        return True
    return _safe_attr(el, "aria-required").lower() == "true"


def _is_cover_letter_label(label: str) -> bool:
    text = (label or "").lower()
    return any(marker in text for marker in COVER_LETTER_LABEL_MARKERS)


# --- mapping with the LLM ------------------------------------------------


def _build_mapping_prompt(fields: list[dict], answers: dict, job_context: dict) -> str:
    safe_answers = {
        k: v for k, v in answers.items()
        if v and not k.startswith("_") and k != "cover_letter_file"
    }
    return (
        f"{HONESTY_INSTRUCTIONS}\n\n{MAPPING_INSTRUCTIONS}\n\n"
        f"Job: {job_context.get('title', '')} at {job_context.get('company', '')}\n"
        f"Company summary: {job_context.get('company_tldr') or 'unknown'}\n\n"
        f"Form fields (JSON):\n{json.dumps(fields, ensure_ascii=False)}\n\n"
        f"Sean's honest answer set (JSON):\n{json.dumps(safe_answers, ensure_ascii=False)}\n\n"
        'Respond with ONLY a JSON object: {"fields": [<one object per field key above>]}. '
        "No prose, no markdown fences, just the JSON object."
    )


def _map_fields(fields: list[dict], answers: dict, job_context: dict, work_dir: Path) -> dict:
    """Map every non-file field to `{"value": ...}`, `{"skip": True}` or
    `{"escalate": "..."}`. Tries the local claude CLI first; whatever it
    fails to cover (including everything, if the call itself fails) falls
    back to the same conservative deterministic mapper the ATS-specific
    handlers use, escalating a required field with no honest answer rather
    than leaving it silently unmapped. A broken claude CLI call never blocks
    the rest of the fill."""
    mapping: dict[str, dict] = {}
    llm_fields = [f for f in fields if f["type"] != "file"]
    if not llm_fields:
        return mapping

    try:
        prompt = _build_mapping_prompt(llm_fields, answers, job_context)
        raw = run_claude(prompt, model=MAPPING_MODEL, cwd=work_dir, timeout=MAPPING_TIMEOUT)
        parsed = extract_json(raw)
        items = parsed.get("fields") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            raise ClaudeCliError("field mapping response had no 'fields' list")
        for item in items:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            key = item["key"]
            if item.get("escalate"):
                mapping[key] = {"escalate": str(item["escalate"])}
            elif item.get("skip"):
                mapping[key] = {"skip": True}
            elif item.get("value") not in (None, ""):
                mapping[key] = {"value": str(item["value"])}
    except ClaudeCliError:
        mapping = {}

    for field in llm_fields:
        if field["key"] in mapping:
            continue
        answer = match_answer(field["label"], answers)
        if answer:
            mapping[field["key"]] = {"value": answer}
        elif field.get("required"):
            mapping[field["key"]] = {
                "escalate": f"No honest answer on file for: {field['label'] or 'unlabeled field'}",
            }
        else:
            mapping[field["key"]] = {"skip": True}
    return mapping


def _apply_mapping(fields: list[dict], elements: dict, mapping: dict) -> list[str]:
    """Fill every field the mapping resolved to a value, and return the
    labels of any field it escalated instead."""
    escalated: list[str] = []
    for field in fields:
        if field["type"] == "file":
            continue
        decision = mapping.get(field["key"], {"skip": True})
        if decision.get("escalate"):
            escalated.append(field["label"] or decision["escalate"])
            continue
        value = decision.get("value")
        if not value:
            continue
        target = elements.get(field["key"])
        if target is None:
            continue
        _fill_one(field["type"], target, value)
    return escalated


def _fill_one(field_type: str, target, value: str) -> None:
    try:
        if field_type == "select":
            _select(target, value)
        elif field_type in ("radio", "checkbox"):
            _check_group(target, value)
        else:
            target.fill(value)
    except Exception:  # noqa: BLE001 - one bad field must never abort the whole form
        pass


def _select(el, value: str) -> None:
    try:
        el.select_option(label=value)
        return
    except Exception:
        pass
    el.select_option(value)


def _check_group(options: list, value: str) -> None:
    target_text = (value or "").strip().lower()
    for option_label, el in options:
        candidate = (option_label or "").strip().lower()
        if candidate == target_text or target_text in candidate:
            try:
                el.check()
            except Exception:
                el.click()
            return


def _fill_file_inputs(fields: list[dict], elements: dict, cv_path, cover_letter_file) -> None:
    for field in fields:
        if field["type"] != "file":
            continue
        el = elements.get(field["key"])
        if el is None:
            continue
        target = cover_letter_file if _is_cover_letter_label(field["label"]) else cv_path
        if not target:
            continue
        try:
            el.set_input_files(str(target))
        except Exception:  # noqa: BLE001 - a failed upload should not abort the rest of the form
            pass


# --- the harvest -> map -> fill -> submit-policy flow ----------------


def _harvest_map_fill(scope, answers, cv_path, auto_submit, job_context, work_dir) -> dict:
    if _detect_captcha(scope):
        return {"status": "needs_help", "reason": "CAPTCHA on form."}
    if _detect_forced_signup(scope):
        return {"status": "needs_help", "reason": "Account signup required before applying."}

    fields, elements = harvest_fields(scope)
    if not fields:
        return {"status": "needs_help", "reason": "Could not locate an application form on this page."}

    mapping = _map_fields(fields, answers, job_context, work_dir)
    escalated = _apply_mapping(fields, elements, mapping)
    _fill_file_inputs(fields, elements, cv_path, answers.get("cover_letter_file"))

    if escalated:
        reason = "Application form has question(s) with no honest basis to answer: " + "; ".join(escalated[:5])
        return {"status": "needs_help", "reason": reason}

    if not auto_submit:
        return {"status": "ready_to_submit", "reason": None}

    submit_button = _find_button_by_text(scope, SUBMIT_TEXT_PATTERNS)
    if submit_button is None:
        return {"status": "needs_help", "reason": "Could not find a submit control for this form."}
    try:
        submit_button.click()
    except Exception as exc:  # noqa: BLE001 - report exactly what went wrong
        return {"status": "needs_help", "reason": f"Could not click the submit control: {exc}"}
    return {"status": "sent", "reason": None}


def _detect_captcha(scope) -> bool:
    try:
        frames = list(getattr(scope, "frames", []) or [])
    except Exception:
        frames = []
    for frame in frames:
        url = (_current_url(frame) or "").lower()
        if any(marker in url for marker in CAPTCHA_URL_MARKERS):
            return True
    try:
        elements = scope.query_selector_all('[class*="captcha" i], [id*="captcha" i]')
    except Exception:
        elements = []
    return any(_visible(el) for el in elements)


def _detect_forced_signup(scope) -> bool:
    """Two visible password fields (password plus confirm-password) on a
    page that is supposed to be a job application means it wants Sean to
    create an account before it will accept one, not log into an existing
    one. A single password field is left alone: that is more often an
    unrelated "log in to see saved applications" widget than a hard block."""
    try:
        passwords = scope.query_selector_all('input[type="password"]')
    except Exception:
        passwords = []
    return len([p for p in passwords if _visible(p)]) >= 2


# --- AllJobs and LinkedIn special cases -----------------------------


def _looks_logged_out(page) -> bool:
    try:
        candidates = page.query_selector_all(CLICKABLE_SELECTOR)
    except Exception:
        return False
    for el in candidates:
        if not _visible(el):
            continue
        text = _text_content(el).lower()
        if any(marker in text for marker in LOGIN_TEXT_MARKERS):
            return True
    return False


def _handle_alljobs(page, answers, cv_path, auto_submit, job_context, work_dir) -> dict:
    """AllJobs' UploadSingle.aspx is the logged-in "send CV" flow: it is
    already the form, no apply affordance to click, but it needs an active
    session on the persistent browser profile."""
    if _looks_logged_out(page):
        return {"status": "needs_help", "reason": "AllJobs login required to send a CV."}
    return _harvest_map_fill(page, answers, cv_path, auto_submit, job_context, work_dir)


def _handle_linkedin(page, answers, cv_path, auto_submit, job_context, work_dir) -> dict:
    if _looks_logged_out(page):
        return {"status": "needs_help", "reason": "LinkedIn login required to apply."}

    external = _find_button_by_text(page, APPLY_TEXT_PATTERNS, exclude=("easy apply",))
    if external is None:
        return {
            "status": "needs_help",
            "reason": EASY_APPLY_ONLY_REASON.format(url=_current_url(page)),
        }
    try:
        external.click()
    except Exception:
        return {"status": "needs_help", "reason": "Could not open the external application link."}
    _settle(page)

    delegated = _delegate_if_known_ats(page, answers, cv_path, auto_submit)
    if delegated is not None:
        return delegated

    scope = page if _looks_like_form(page) else _find_form_frame(page)
    if scope is None:
        popup = _newest_popup(page)
        scope = popup if popup is not None and _looks_like_form(popup) else None
    if scope is None:
        return {
            "status": "needs_help",
            "reason": "Could not locate an application form after following the external apply link.",
        }
    return _harvest_map_fill(scope, answers, cv_path, auto_submit, job_context, work_dir)


# --- small duck-typed helpers, tolerant of both real Playwright objects
# and the fakes the tests use ---------------------------------------


def _current_url(scope) -> str:
    try:
        return scope.url or ""
    except Exception:
        return ""


def _visible(el) -> bool:
    is_visible = getattr(el, "is_visible", None)
    if not callable(is_visible):
        return True
    try:
        return bool(is_visible())
    except Exception:
        return True


def _safe_attr(el, name: str) -> str:
    try:
        return el.get_attribute(name) or ""
    except Exception:
        return ""


def _has_attr(el, name: str) -> bool:
    try:
        return el.get_attribute(name) is not None
    except Exception:
        return False


def _text_content(el) -> str:
    try:
        return re.sub(r"\s+", " ", (el.text_content() or "")).strip()
    except Exception:
        return ""

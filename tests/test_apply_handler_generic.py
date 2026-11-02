"""Tests for the generic browser-plus-LLM apply handler. Every Playwright
object is a small fake: no real browser, no network, and every claude CLI
call is monkeypatched (`generic.run_claude` for field mapping,
`answers_module.run_claude` for the cover letter the orchestrator generates
before any handler runs)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from autojob import db
from autojob.apply import answers as answers_module
from autojob.apply import apply as apply_module
from autojob.apply.handlers import generic, greenhouse


class FakeElement:
    def __init__(self, *, attrs=None, text="", visible=True, options=None, excluded=False):
        self.excluded = excluded
        self.attrs = attrs or {}
        self._text = text
        self.visible = visible
        self.value = None
        self.clicked = False
        self.checked = False
        self.files = None
        self.selected = None
        self._options = options or []

    def get_attribute(self, name):
        return self.attrs.get(name)

    def is_visible(self):
        return self.visible

    def fill(self, value):
        self.value = value

    def click(self):
        self.clicked = True

    def check(self):
        self.checked = True

    def set_input_files(self, path):
        self.files = path

    def select_option(self, value=None, label=None):
        self.selected = label if label is not None else value

    def evaluate(self, js):
        return self.excluded

    def text_content(self):
        return self._text

    def query_selector_all(self, selector):
        if selector == "option":
            return self._options
        return []


class FakeFormPage:
    def __init__(
        self,
        *,
        inputs=None,
        textareas=None,
        selects=None,
        clickable=None,
        labels=None,
        url="https://careers.example.com/job/1",
        frames=None,
        passwords=None,
        captchas=None,
    ):
        self.url = url
        self._inputs = inputs or []
        self._textareas = textareas or []
        self._selects = selects or []
        self._clickable = clickable or []
        self._labels = labels or {}
        self.frames = [] if frames is None else frames
        self._passwords = passwords or []
        self._captchas = captchas or []

    def query_selector(self, selector):
        if selector.startswith('label[for="'):
            field_id = selector[len('label[for="'):-2]
            return self._labels.get(field_id)
        return None

    def query_selector_all(self, selector):
        if selector == generic.INPUT_SELECTOR:
            return self._inputs
        if selector == "textarea":
            return self._textareas
        if selector == "select":
            return self._selects
        if selector == generic.CLICKABLE_SELECTOR:
            return self._clickable
        if selector == 'input[type="password"]':
            return self._passwords
        if selector == '[class*="captcha" i], [id*="captcha" i]':
            return self._captchas
        return []

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass


def _mapping_response(items: list[dict]) -> str:
    return json.dumps({"fields": items})


# --- apply-affordance detection -----------------------------------------


def test_find_apply_affordance_detects_english_button():
    apply_btn = FakeElement(text="Apply Now")
    other = FakeElement(text="Learn more")
    page = FakeFormPage(clickable=[other, apply_btn])
    assert generic.find_apply_affordance(page) is apply_btn


def test_find_apply_affordance_detects_hebrew_link():
    link = FakeElement(text="הגש מועמדות")
    page = FakeFormPage(clickable=[link])
    assert generic.find_apply_affordance(page) is link


def test_find_apply_affordance_returns_none_when_nothing_matches():
    page = FakeFormPage(clickable=[FakeElement(text="Learn more"), FakeElement(text="About us")])
    assert generic.find_apply_affordance(page) is None


def test_find_apply_affordance_skips_hidden_elements():
    hidden = FakeElement(text="Apply now", visible=False)
    page = FakeFormPage(clickable=[hidden])
    assert generic.find_apply_affordance(page) is None


def test_find_apply_affordance_matches_aria_label():
    el = FakeElement(attrs={"aria-label": "Submit application"}, text="")
    page = FakeFormPage(clickable=[el])
    assert generic.find_apply_affordance(page) is el


# --- harvesting -----------------------------------------------------------


def test_harvest_fields_collects_text_field_with_label():
    email_input = FakeElement(attrs={"id": "email", "type": "email"})
    page = FakeFormPage(inputs=[email_input], labels={"email": FakeElement(text="Email address")})
    fields, elements = generic.harvest_fields(page)
    assert len(fields) == 1
    assert fields[0]["label"] == "Email address"
    assert fields[0]["type"] == "email"
    assert elements[fields[0]["key"]] is email_input


def test_harvest_fields_uses_placeholder_when_no_label_element():
    el = FakeElement(attrs={"id": "phone", "type": "tel", "placeholder": "Phone number"})
    page = FakeFormPage(inputs=[el])
    fields, _ = generic.harvest_fields(page)
    assert fields[0]["label"] == "Phone number"


def test_harvest_fields_groups_radio_buttons_by_name():
    yes = FakeElement(attrs={"name": "relocate", "type": "radio", "id": "relocate_yes"})
    no = FakeElement(attrs={"name": "relocate", "type": "radio", "id": "relocate_no"})
    page = FakeFormPage(
        inputs=[yes, no],
        labels={"relocate_yes": FakeElement(text="Yes"), "relocate_no": FakeElement(text="No")},
    )
    fields, elements = generic.harvest_fields(page)
    assert len(fields) == 1
    assert fields[0]["type"] == "radio"
    assert fields[0]["options"] == ["Yes", "No"]
    assert len(elements[fields[0]["key"]]) == 2


def test_harvest_fields_skips_hidden_fields():
    visible = FakeElement(attrs={"id": "name", "type": "text"})
    hidden = FakeElement(attrs={"id": "hidden_field", "type": "text"}, visible=False)
    page = FakeFormPage(inputs=[visible, hidden], labels={"name": FakeElement(text="Full name")})
    fields, _ = generic.harvest_fields(page)
    assert len(fields) == 1


def test_harvest_fields_collects_select_options():
    country_el = FakeElement(
        attrs={"id": "country"},
        options=[FakeElement(text="Israel"), FakeElement(text="Other")],
    )
    page = FakeFormPage(selects=[country_el], labels={"country": FakeElement(text="Country")})
    fields, _ = generic.harvest_fields(page)
    assert fields[0]["type"] == "select"
    assert fields[0]["options"] == ["Israel", "Other"]


def test_harvest_fields_marks_required_from_star_in_label():
    el = FakeElement(attrs={"id": "salary"})
    page = FakeFormPage(inputs=[el], labels={"salary": FakeElement(text="Expected salary*")})
    fields, _ = generic.harvest_fields(page)
    assert fields[0]["required"] is True


# --- filling from a mapping -----------------------------------------------


def test_apply_mapping_fills_text_select_and_radio_fields():
    text_el = FakeElement()
    select_el = FakeElement()
    yes_el = FakeElement()
    no_el = FakeElement()
    fields = [
        {"key": "f0", "label": "Email", "type": "email", "required": True},
        {"key": "f1", "label": "Country", "type": "select", "required": False, "options": ["Israel", "Other"]},
        {"key": "f2", "label": "Relocate?", "type": "radio", "required": False, "options": ["Yes", "No"]},
    ]
    elements = {"f0": text_el, "f1": select_el, "f2": [("Yes", yes_el), ("No", no_el)]}
    mapping = {
        "f0": {"value": "sean@example.com"},
        "f1": {"value": "Israel"},
        "f2": {"value": "Yes"},
    }
    escalated = generic._apply_mapping(fields, elements, mapping)
    assert escalated == []
    assert text_el.value == "sean@example.com"
    assert select_el.selected == "Israel"
    assert yes_el.checked is True
    assert no_el.checked is False


def test_apply_mapping_collects_escalated_labels_and_skips_skipped_fields():
    optional_el = FakeElement()
    fields = [
        {"key": "f0", "label": "Years with Kubernetes?*", "type": "text", "required": True},
        {"key": "f1", "label": "Portfolio (optional)", "type": "text", "required": False},
    ]
    elements = {"f0": FakeElement(), "f1": optional_el}
    mapping = {
        "f0": {"escalate": "No honest basis for a Kubernetes-specific number."},
        "f1": {"skip": True},
    }
    escalated = generic._apply_mapping(fields, elements, mapping)
    assert escalated == ["Years with Kubernetes?*"]
    assert optional_el.value is None


def test_fill_file_inputs_uploads_cv_and_cover_letter_by_label():
    resume_el = FakeElement(attrs={"type": "file"})
    cover_el = FakeElement(attrs={"type": "file"})
    fields = [
        {"key": "f0", "label": "Resume", "type": "file"},
        {"key": "f1", "label": "Cover Letter", "type": "file"},
    ]
    elements = {"f0": resume_el, "f1": cover_el}
    generic._fill_file_inputs(fields, elements, "/tmp/cv.pdf", "/tmp/cover.txt")
    assert resume_el.files == "/tmp/cv.pdf"
    assert cover_el.files == "/tmp/cover.txt"


# --- mapping with the LLM (and its deterministic fallback) ---------------


def test_map_fields_uses_llm_value_when_provided(monkeypatch, tmp_path):
    monkeypatch.setattr(
        generic, "run_claude",
        lambda *a, **kw: _mapping_response([{"key": "f0", "value": "Tel Aviv"}]),
    )
    fields = [{"key": "f0", "label": "City", "type": "text", "required": False}]
    mapping = generic._map_fields(fields, {"city": "Tel Aviv"}, {}, tmp_path)
    assert mapping["f0"] == {"value": "Tel Aviv"}


def test_map_fields_falls_back_to_deterministic_mapper_when_claude_cli_fails(monkeypatch, tmp_path):
    def _raise(*a, **kw):
        raise generic.ClaudeCliError("claude CLI unavailable")

    monkeypatch.setattr(generic, "run_claude", _raise)
    fields = [
        {"key": "f0", "label": "Email", "type": "email", "required": True},
        {"key": "f1", "label": "Years of experience with Kubernetes?*", "type": "text", "required": True},
    ]
    mapping = generic._map_fields(fields, {"email": "sean@example.com"}, {}, tmp_path)
    assert mapping["f0"] == {"value": "sean@example.com"}
    assert "escalate" in mapping["f1"]


# --- CAPTCHA and forced-signup detection ----------------------------------


def test_detect_captcha_via_iframe_url_marker():
    class FrameLike:
        url = "https://www.google.com/recaptcha/api2/anchor"

    page = FakeFormPage(frames=[FrameLike()])
    assert generic._detect_captcha(page) is True


def test_detect_captcha_false_when_nothing_matches():
    page = FakeFormPage(frames=[])
    assert generic._detect_captcha(page) is False


def test_detect_forced_signup_with_two_password_fields():
    page = FakeFormPage(passwords=[FakeElement(), FakeElement()])
    assert generic._detect_forced_signup(page) is True


def test_detect_forced_signup_ignores_a_single_password_field():
    page = FakeFormPage(passwords=[FakeElement()])
    assert generic._detect_forced_signup(page) is False


# --- the full apply() flow, against a fake page ---------------------------


def test_generic_apply_holds_when_auto_submit_false(monkeypatch, config):
    email_el = FakeElement(attrs={"id": "email", "type": "email"})
    phone_el = FakeElement(attrs={"id": "phone", "type": "tel"})
    submit_el = FakeElement(text="Submit application")
    page = FakeFormPage(
        inputs=[email_el, phone_el, FakeElement(attrs={"id": "fullname", "type": "text"})],
        clickable=[submit_el],
        labels={"email": FakeElement(text="Email"), "phone": FakeElement(text="Phone")},
        url="https://careers.example.com/job/9",
    )
    monkeypatch.setattr(
        generic, "run_claude",
        lambda *a, **kw: _mapping_response(
            [{"key": "f0", "value": "sean@example.com"}, {"key": "f1", "value": "0500000000"}]
        ),
    )

    result = generic.apply(page, {"email": "sean@example.com", "phone": "0500000000"}, None, auto_submit=False)

    assert result == {"status": "ready_to_submit", "reason": None}
    assert email_el.value == "sean@example.com"
    assert submit_el.clicked is False


def test_generic_apply_submits_when_auto_submit_true(monkeypatch, config):
    email_el = FakeElement(attrs={"id": "email", "type": "email"})
    phone_el = FakeElement(attrs={"id": "phone", "type": "tel"})
    submit_el = FakeElement(text="Submit application")
    page = FakeFormPage(
        inputs=[email_el, phone_el, FakeElement(attrs={"id": "fullname", "type": "text"})],
        clickable=[submit_el],
        labels={"email": FakeElement(text="Email"), "phone": FakeElement(text="Phone")},
        url="https://careers.example.com/job/9",
    )
    monkeypatch.setattr(
        generic, "run_claude",
        lambda *a, **kw: _mapping_response(
            [{"key": "f0", "value": "sean@example.com"}, {"key": "f1", "value": "0500000000"}]
        ),
    )

    result = generic.apply(page, {"email": "sean@example.com", "phone": "0500000000"}, None, auto_submit=True)

    assert result == {"status": "sent", "reason": None}
    assert submit_el.clicked is True


def test_generic_apply_escalates_when_no_form_can_be_found(config):
    page = FakeFormPage(inputs=[], clickable=[], url="https://careers.example.com/job/empty")
    result = generic.apply(page, {}, None, auto_submit=False)
    assert result["status"] == "needs_help"
    assert "locate an application form" in result["reason"]


def test_generic_apply_escalates_on_captcha(config):
    class FrameLike:
        url = "https://www.google.com/recaptcha/api2/anchor"

    email_el = FakeElement(attrs={"id": "email", "type": "email"})
    phone_el = FakeElement(attrs={"id": "phone", "type": "tel"})
    page = FakeFormPage(inputs=[email_el, phone_el, FakeElement(attrs={"id": "fullname", "type": "text"})], frames=[FrameLike()], url="https://careers.example.com/job/1")
    result = generic.apply(page, {}, None, auto_submit=False)
    assert result == {"status": "needs_help", "reason": "CAPTCHA on form."}


def test_generic_apply_escalates_on_forced_signup(config):
    email_el = FakeElement(attrs={"id": "email", "type": "email"})
    phone_el = FakeElement(attrs={"id": "phone", "type": "tel"})
    page = FakeFormPage(
        inputs=[email_el, phone_el, FakeElement(attrs={"id": "fullname", "type": "text"})],
        passwords=[FakeElement(), FakeElement()],
        url="https://careers.example.com/job/1",
    )
    result = generic.apply(page, {}, None, auto_submit=False)
    assert result == {"status": "needs_help", "reason": "Account signup required before applying."}


def test_generic_apply_delegates_to_a_specific_handler_after_following_the_apply_button(monkeypatch, config):
    """A company's own "Apply" button often just redirects to a Greenhouse
    (or Lever/Comeet) hosted form. Once clicking it lands the page there,
    the generic handler hands off to that deterministic handler instead of
    harvesting the redirected form itself."""

    class RedirectingPage:
        def __init__(self):
            self.url = "https://careers.example.com/job/1"
            self.frames = []
            self._navigated = False

        def query_selector_all(self, selector):
            if selector == generic.CLICKABLE_SELECTOR:
                return [] if self._navigated else [self._make_apply_button()]
            if selector == generic.INPUT_SELECTOR:
                if self._navigated:
                    return [
                        FakeElement(attrs={"id": "first_name", "type": "text"}),
                        FakeElement(attrs={"id": "email", "type": "email"}),
                        FakeElement(attrs={"id": "last_name", "type": "text"}),
                    ]
                return []
            return []

        def query_selector(self, selector):
            return None

        def wait_for_load_state(self, *a, **kw):
            pass

        def _make_apply_button(self):
            outer = self

            class _Button(FakeElement):
                def click(_self):
                    _self.clicked = True
                    outer.url = "https://job-boards.greenhouse.io/acme/jobs/1"
                    outer._navigated = True

            return _Button(text="Apply now")

    page = RedirectingPage()
    fixed_result = {"status": "ready_to_submit", "reason": None}
    calls = []

    def fake_greenhouse_apply(p, answers, cv_path, auto_submit):
        calls.append(p)
        return dict(fixed_result)

    monkeypatch.setattr(greenhouse, "apply", fake_greenhouse_apply)

    result = generic.apply(page, {"full_name": "Sean Larch"}, None, auto_submit=False)

    assert result == fixed_result
    assert calls == [page]


# --- form detection threshold, excluded containers, LinkedIn ---------------


def _field(field_id, input_type="text", **kw):
    return FakeElement(attrs={"id": field_id, "type": input_type}, **kw)


def test_a_lone_newsletter_input_is_not_a_form(config):
    newsletter = _field("email", "email", excluded=True)
    page = FakeFormPage(inputs=[newsletter], labels={"email": FakeElement(text="Email")})
    result = generic.apply(page, {"email": "s@example.com"}, None, auto_submit=False)
    assert result == {"status": "needs_help", "reason": "Could not locate an application form on this page."}
    assert newsletter.value is None


def test_two_fields_without_a_file_input_are_not_a_form(config):
    page = FakeFormPage(inputs=[_field("a", "text"), _field("b", "email")])
    result = generic.apply(page, {}, None, auto_submit=False)
    assert result["reason"] == "Could not locate an application form on this page."


def test_name_email_and_file_is_a_form(monkeypatch, config, tmp_path):
    name_el, email_el, file_el = _field("name"), _field("email", "email"), _field("cv", "file")
    page = FakeFormPage(
        inputs=[name_el, email_el, file_el],
        labels={"name": FakeElement(text="Full name"), "email": FakeElement(text="Email"),
                "cv": FakeElement(text="Resume")},
    )
    monkeypatch.setattr(
        generic, "run_claude",
        lambda *a, **kw: _mapping_response([{"key": "f0", "value": "Sean"}, {"key": "f1", "value": "s@example.com"}]),
    )
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF")
    result = generic.apply(page, {"full_name": "Sean"}, str(cv), auto_submit=False)
    assert result["status"] == "ready_to_submit"
    assert name_el.value == "Sean" and file_el.files == str(cv)


def test_search_box_in_excluded_container_is_skipped(config):
    search = _field("q", "search", excluded=True)
    page = FakeFormPage(inputs=[search, _field("a"), _field("b"), _field("c", "email")])
    fields, elements = generic.harvest_fields(page)
    assert len(fields) == 3 and search not in elements.values()


def test_linkedin_easy_apply_only_listing_is_not_automated(config):
    easy = FakeElement(text="Easy Apply")
    page = FakeFormPage(clickable=[easy], url="https://www.linkedin.com/jobs/view/12345")
    result = generic.apply(page, {}, None, auto_submit=True)
    assert result["status"] == "needs_help"
    assert "https://www.linkedin.com/jobs/view/12345" in result["reason"]
    assert "Easy Apply" in result["reason"]
    assert easy.clicked is False


def test_linkedin_external_apply_link_is_clicked(config):
    easy = FakeElement(text="Easy Apply")
    external = FakeElement(text="Apply")
    page = FakeFormPage(clickable=[easy, external], url="https://www.linkedin.com/jobs/view/12345")
    generic.apply(page, {}, None, auto_submit=False)
    assert external.clicked is True and easy.clicked is False


# --- AllJobs and LinkedIn login-required escalation -----------------------


def test_alljobs_escalates_when_logged_out(config):
    login_link = FakeElement(text="התחברות")
    page = FakeFormPage(
        clickable=[login_link],
        url="https://www.alljobs.co.il/Search/UploadSingle.aspx?jobid=123",
    )
    result = generic.apply(page, {}, None, auto_submit=False)
    assert result["status"] == "needs_help"
    assert "AllJobs login" in result["reason"]


def test_linkedin_escalates_when_logged_out(config):
    login_link = FakeElement(text="Sign in")
    page = FakeFormPage(
        clickable=[login_link],
        url="https://www.linkedin.com/jobs/view/12345",
    )
    result = generic.apply(page, {}, None, auto_submit=False)
    assert result["status"] == "needs_help"
    assert "LinkedIn login" in result["reason"]


# --- the orchestrator: exactly one help_item, but still a screenshot -----


class FakeOrchestratorPage:
    def __init__(self, form_page: FakeFormPage):
        self._form_page = form_page
        self.url = form_page.url

    def goto(self, url, wait_until=None):
        self.url = url

    def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"fake png")

    def content(self):
        return "<html></html>"

    def pdf(self, path):
        Path(path).write_bytes(b"%PDF-1.4 fake")

    def __getattr__(self, name):
        return getattr(self._form_page, name)


def _seed_job(config, url: str):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Cato Networks")
        job_id = db.create_job(
            conn, source="manual", url=url, title="Junior Penetration Tester",
            company_id=company_id, status="queued",
        )
        job = db.get_job_with_company(conn, job_id)
    finally:
        conn.close()
    return job


def _patch_cover_letter(monkeypatch):
    payload = {"cover_letter": "Dear Cato Networks team, honest letter.", "screening_answers": {}}
    monkeypatch.setattr(answers_module, "run_claude", lambda *a, **kw: json.dumps(payload))


def test_apply_to_job_via_generic_escalates_one_help_item_but_still_screenshots(monkeypatch, config):
    email_el = FakeElement(attrs={"id": "email", "type": "email"})
    eligibility_el = FakeElement(attrs={"id": "eligibility", "type": "text"})
    form_page = FakeFormPage(
        inputs=[email_el, eligibility_el, FakeElement(attrs={"id": "fullname", "type": "text"})],
        labels={
            "email": FakeElement(text="Email"),
            "eligibility": FakeElement(text="Are you authorized to work in Israel without sponsorship?*"),
        },
        url="https://catonetworks.com/careers/jobs/123",
    )
    page = FakeOrchestratorPage(form_page)

    @contextmanager
    def fake_browser_page(cfg, headless=True, visible=False):
        yield page

    monkeypatch.setattr(apply_module, "browser_page", fake_browser_page)
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, p: generic)
    monkeypatch.setattr(
        generic, "run_claude",
        lambda *a, **kw: _mapping_response([
            {"key": "f0", "value": "sean@example.com"},
            {"key": "f1", "escalate": "No honest basis: Sean's work-authorization status isn't in his profile."},
        ]),
    )
    _patch_cover_letter(monkeypatch)

    job = _seed_job(config, "https://catonetworks.com/careers/jobs/123")
    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "needs_help"
    assert email_el.value == "sean@example.com"  # everything answerable still got filled in

    conn = db.get_connection(config)
    try:
        job_row = db.get_job(conn, job["id"])
        help_items = db.list_open_help_items(conn)
        application = db.get_application(conn, result["application_id"])
        proofs = db.list_proofs(conn, result["application_id"])
    finally:
        conn.close()

    assert job_row["status"] == "needs_help"
    assert len(help_items) == 1
    assert "authorized to work" in help_items[0]["reason"]
    assert application["channel"] == "generic:catonetworks.com"
    assert [p["kind"] for p in proofs] == ["screenshot"]


def test_apply_to_job_via_generic_holds_as_ready_to_submit_and_never_submits(monkeypatch, config):
    email_el = FakeElement(attrs={"id": "email", "type": "email"})
    phone_el = FakeElement(attrs={"id": "phone", "type": "tel"})
    submit_el = FakeElement(text="Submit application")
    form_page = FakeFormPage(
        inputs=[email_el, phone_el, FakeElement(attrs={"id": "fullname", "type": "text"})],
        clickable=[submit_el],
        labels={"email": FakeElement(text="Email"), "phone": FakeElement(text="Phone")},
        url="https://catonetworks.com/careers/jobs/456",
    )
    page = FakeOrchestratorPage(form_page)

    @contextmanager
    def fake_browser_page(cfg, headless=True, visible=False):
        yield page

    monkeypatch.setattr(apply_module, "browser_page", fake_browser_page)
    monkeypatch.setattr(apply_module, "pick_handler", lambda url, p: generic)
    monkeypatch.setattr(
        generic, "run_claude",
        lambda *a, **kw: _mapping_response(
            [{"key": "f0", "value": "sean@example.com"}, {"key": "f1", "value": "0500000000"}]
        ),
    )
    _patch_cover_letter(monkeypatch)

    job = _seed_job(config, "https://catonetworks.com/careers/jobs/456")
    result = apply_module.apply_to_job(job, auto_submit=False)

    assert result["status"] == "ready_to_submit"
    assert submit_el.clicked is False


# --- re-queue maintenance ---------------------------------------------


def test_requeue_stale_no_handler_jobs_moves_only_the_no_handler_reason(config):
    db.init_db(config)
    conn = db.get_connection(config)
    try:
        company_id = db.get_or_create_company(conn, "Acme")
        stale_job_id = db.create_job(
            conn, source="manual", url="https://careers.example.com/job/1", title="Pentester",
            company_id=company_id, status="needs_help",
        )
        db.create_help_item(
            conn, job_id=stale_job_id,
            reason="No apply handler recognizes this ATS yet (https://careers.example.com/job/1).",
        )
        real_help_job_id = db.create_job(
            conn, source="manual", url="https://careers.example.com/job/2", title="SOC Analyst",
            company_id=company_id, status="needs_help",
        )
        db.create_help_item(conn, job_id=real_help_job_id, reason="CAPTCHA on form.")

        count = apply_module.requeue_stale_no_handler_jobs(conn)

        assert count == 1
        stale_job = db.get_job(conn, stale_job_id)
        real_help_job = db.get_job(conn, real_help_job_id)
        assert stale_job["status"] == "queued"
        assert real_help_job["status"] == "needs_help"
        remaining = db.list_open_help_items(conn)
        assert len(remaining) == 1
        assert remaining[0]["reason"] == "CAPTCHA on form."

        assert apply_module.requeue_stale_no_handler_jobs(conn) == 0  # idempotent
    finally:
        conn.close()

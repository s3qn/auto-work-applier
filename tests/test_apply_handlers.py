"""Apply handler and router tests. Every Playwright object is a small fake:
no real browser, no network. Each handler test checks that the expected
fields get filled and, critically, that a submit control is never clicked
when auto_submit is false."""

from __future__ import annotations

from autojob.apply.handlers import comeet, greenhouse, lever
from autojob.apply.router import pick_handler, pick_handler_by_url


class FakeElement:
    def __init__(self, attrs=None, text=""):
        self.attrs = attrs or {}
        self._text = text
        self.value = None
        self.clicked = False
        self.files = None

    def get_attribute(self, name):
        return self.attrs.get(name)

    def fill(self, value):
        self.value = value

    def click(self):
        self.clicked = True

    def set_input_files(self, path):
        self.files = path

    def text_content(self):
        return self._text

    def input_value(self):
        return self.value


# --- greenhouse -------------------------------------------------------

class FakeGreenhousePage:
    def __init__(self, custom_questions=None, has_submit=True):
        self.url = "https://job-boards.greenhouse.io/acme/jobs/123"
        self.fields = {
            "#first_name": FakeElement(),
            "#last_name": FakeElement(),
            "#email": FakeElement(),
            "#phone": FakeElement(),
            "#candidate-location": FakeElement(),
            "#country": FakeElement(),
            "#resume": FakeElement(attrs={"type": "file"}),
        }
        self.labels = {}
        self.question_fields = []
        for field_id, label, field_type in custom_questions or []:
            self.question_fields.append(FakeElement(attrs={"id": field_id, "type": field_type}))
            self.labels[field_id] = label
        self.submit_button = FakeElement() if has_submit else None

    def query_selector(self, selector):
        if selector.startswith('label[for="'):
            field_id = selector[len('label[for="'):-2]
            return FakeElement(text=self.labels[field_id]) if field_id in self.labels else None
        if selector == 'button[type="submit"]':
            return self.submit_button
        return self.fields.get(selector)

    def query_selector_all(self, selector):
        if selector == 'input[id^="question_"], textarea[id^="question_"]':
            return self.question_fields
        return []


def test_greenhouse_matches_url():
    assert greenhouse.matches("https://job-boards.greenhouse.io/acme/jobs/123") is True
    assert greenhouse.matches("https://boards.greenhouse.io/acme/jobs/123") is True
    assert greenhouse.matches("https://jobs.lever.co/acme/xyz") is False


def test_greenhouse_fills_standard_fields_and_holds_when_auto_submit_false():
    page = FakeGreenhousePage()
    answers = {
        "full_name": "Sean Larch", "email": "sean@example.com", "phone": "0500000000",
        "city": "Tel Aviv", "country": "Israel", "cover_letter": "Dear team...",
    }
    result = greenhouse.apply(page, answers, "/tmp/cv.pdf", auto_submit=False)
    assert result == {"status": "ready_to_submit", "reason": None}
    assert page.fields["#first_name"].value == "Sean"
    assert page.fields["#last_name"].value == "Larch"
    assert page.fields["#email"].value == "sean@example.com"
    assert page.fields["#candidate-location"].value == "Tel Aviv"
    assert page.fields["#resume"].files == "/tmp/cv.pdf"
    assert page.submit_button.clicked is False


def test_greenhouse_escalates_unmapped_required_question_instead_of_guessing():
    page = FakeGreenhousePage(
        custom_questions=[("question_1", "How comfortable are you with PostgreSQL?*", "text")]
    )
    answers = {"full_name": "Sean Larch", "email": "sean@example.com"}
    result = greenhouse.apply(page, answers, None, auto_submit=False)
    assert result["status"] == "needs_help"
    assert "PostgreSQL" in result["reason"]
    assert page.submit_button.clicked is False


def test_greenhouse_answers_an_obvious_required_question_from_the_bank():
    page = FakeGreenhousePage(
        custom_questions=[("question_1", "How many years of professional experience do you have?*", "text")]
    )
    answers = {"full_name": "Sean Larch", "email": "sean@example.com", "years_experience": "0 to 1 years"}
    result = greenhouse.apply(page, answers, None, auto_submit=False)
    assert result["status"] == "ready_to_submit"
    assert page.question_fields[0].value == "0 to 1 years"


def test_greenhouse_submits_when_auto_submit_true():
    page = FakeGreenhousePage()
    result = greenhouse.apply(page, {"full_name": "Sean Larch", "email": "s@example.com"}, None, auto_submit=True)
    assert result == {"status": "sent", "reason": None}
    assert page.submit_button.clicked is True


def test_greenhouse_needs_help_when_no_submit_button_found():
    page = FakeGreenhousePage(has_submit=False)
    result = greenhouse.apply(page, {"full_name": "Sean Larch", "email": "s@example.com"}, None, auto_submit=True)
    assert result["status"] == "needs_help"


# --- lever --------------------------------------------------------------

class FakeFieldArea:
    def __init__(self, field=None):
        self._field = field

    def query_selector(self, selector):
        return self._field


class FakeQuestion:
    def __init__(self, label, field=None, required=False):
        self._label_el = FakeElement(text=label)
        self._field_area = FakeFieldArea(field)
        self._required = required

    def query_selector(self, selector):
        if selector == ".application-field":
            return self._field_area
        if selector == ".application-label":
            return self._label_el
        if selector == ".required":
            return FakeElement() if self._required else None
        return None


class FakeLeverPage:
    def __init__(self, url="https://jobs.lever.co/acme/xyz/apply", questions=None, has_submit=True):
        self.url = url
        self.goto_calls = []
        self.fields = {
            'input[name="name"]': FakeElement(),
            'input[name="email"]': FakeElement(),
            'input[name="phone"]': FakeElement(),
            'input[name="location"], #location-input': FakeElement(),
            'input[name="urls[LinkedIn]"]': FakeElement(),
            'input[name="urls[GitHub]"]': FakeElement(),
            'input[name="urls[Portfolio]"]': FakeElement(),
            'input[name="resume"], #resume-upload-input': FakeElement(attrs={"type": "file"}),
        }
        self.questions = questions or []
        self.submit_button = FakeElement() if has_submit else None

    def goto(self, url, wait_until=None):
        self.url = url
        self.goto_calls.append(url)

    def query_selector(self, selector):
        if selector == '#btn-submit, [data-qa="btn-submit"]':
            return self.submit_button
        return self.fields.get(selector)

    def query_selector_all(self, selector):
        if selector == "li.application-question":
            return self.questions
        return []


def test_lever_matches_url():
    assert lever.matches("https://jobs.lever.co/acme/xyz") is True
    assert lever.matches("https://boards.greenhouse.io/acme") is False


def test_lever_navigates_to_the_apply_url_when_not_already_there():
    page = FakeLeverPage(url="https://jobs.lever.co/acme/xyz")
    lever.apply(page, {"full_name": "Sean Larch", "email": "sean@example.com"}, None, auto_submit=False)
    assert page.goto_calls == ["https://jobs.lever.co/acme/xyz/apply"]


def test_lever_fills_standard_fields_and_holds_when_auto_submit_false():
    page = FakeLeverPage()
    answers = {
        "full_name": "Sean Larch", "email": "sean@example.com", "phone": "0500000000",
        "city": "Tel Aviv", "linkedin_url": "https://linkedin.com/in/sean",
    }
    result = lever.apply(page, answers, "/tmp/cv.pdf", auto_submit=False)
    assert result == {"status": "ready_to_submit", "reason": None}
    assert page.fields['input[name="name"]'].value == "Sean Larch"
    assert page.fields['input[name="urls[LinkedIn]"]'].value == "https://linkedin.com/in/sean"
    assert page.fields['input[name="resume"], #resume-upload-input'].files == "/tmp/cv.pdf"
    assert page.submit_button.clicked is False


def test_lever_does_not_double_flag_standard_fields_wrapped_as_questions():
    # Lever also wraps name/email/resume in their own application-question
    # elements; the loop must recognize and skip these by field name rather
    # than treating them as unanswerable custom questions.
    name_field = FakeElement(attrs={"name": "name", "type": "text"})
    email_field = FakeElement(attrs={"name": "email", "type": "email"})
    questions = [
        FakeQuestion("Full name✱", field=name_field, required=True),
        FakeQuestion("Email✱", field=email_field, required=True),
    ]
    page = FakeLeverPage(questions=questions)
    result = lever.apply(
        page, {"full_name": "Sean Larch", "email": "sean@example.com"}, None, auto_submit=False
    )
    assert result["status"] == "ready_to_submit"


def test_lever_escalates_unmapped_required_multiple_choice_question():
    field = FakeElement(attrs={"name": "cards[abc][field0]", "type": "radio"})
    question = FakeQuestion(
        "Are you currently located in Canada and legally allowed to work there?✱",
        field=field,
        required=True,
    )
    page = FakeLeverPage(questions=[question])
    result = lever.apply(page, {"full_name": "Sean Larch", "email": "s@example.com"}, None, auto_submit=False)
    assert result["status"] == "needs_help"
    assert "Canada" in result["reason"]


def test_lever_submits_when_auto_submit_true():
    page = FakeLeverPage()
    result = lever.apply(page, {"full_name": "Sean Larch", "email": "s@example.com"}, None, auto_submit=True)
    assert result == {"status": "sent", "reason": None}
    assert page.submit_button.clicked is True


# --- comeet ---------------------------------------------------------------

class FakeComeetFrame:
    def __init__(self, url, fields=None, questions=None, has_submit=True):
        self.url = url
        self.fields = fields or {}
        self.questions = questions or []
        self.submit_button = FakeElement() if has_submit else None

    def query_selector(self, selector):
        if selector in ("button.applyButton", '[ng-click="apply()"]'):
            return self.submit_button
        return self.fields.get(selector)

    def query_selector_all(self, selector):
        if selector == ".customQuestion, .question":
            return self.questions
        return []


class FakeComeetPage:
    def __init__(self, frame=None, requires_click=False):
        self._frame = frame
        self._requires_click = requires_click
        self._clicked = False
        self._apply_button = FakeElement()

    @property
    def frames(self):
        if self._frame is None:
            return []
        if self._requires_click and not self._clicked:
            return []
        return [self._frame]

    def query_selector(self, selector):
        if "Apply for this job" in selector:
            outer = self

            class _Button(FakeElement):
                def click(_self):
                    _self.clicked = True
                    outer._clicked = True

            self._apply_button = _Button()
            return self._apply_button
        return None

    def wait_for_timeout(self, ms):
        pass


def test_comeet_matches_url():
    assert comeet.matches("https://www.comeet.com/jobs/acme/D5.005/pentester/1A.2B3") is True
    assert comeet.matches("https://jobs.lever.co/acme") is False


def test_comeet_fills_fields_from_the_already_present_iframe():
    frame = FakeComeetFrame(
        url="https://www.comeet.co/jobs/D5/1A/apply?token=x",
        fields={
            "#inputFirstName": FakeElement(),
            "#inputLastName": FakeElement(),
            "#inputEmail": FakeElement(),
            "#inputTel": FakeElement(),
            "#inputLink": FakeElement(),
            "#inputNote": FakeElement(),
            "#cv": FakeElement(attrs={"type": "file"}),
            "#coverLetter": FakeElement(attrs={"type": "file"}),
        },
    )
    page = FakeComeetPage(frame=frame)
    answers = {
        "full_name": "Sean Larch", "email": "sean@example.com", "phone": "0500000000",
        "linkedin_url": "https://linkedin.com/in/sean", "cover_letter": "Dear team...",
        "cover_letter_file": "/tmp/cover.txt",
    }
    result = comeet.apply(page, answers, "/tmp/cv.pdf", auto_submit=False)
    assert result == {"status": "ready_to_submit", "reason": None}
    assert frame.fields["#inputFirstName"].value == "Sean"
    assert frame.fields["#inputLastName"].value == "Larch"
    assert frame.fields["#cv"].files == "/tmp/cv.pdf"
    assert frame.fields["#coverLetter"].files == "/tmp/cover.txt"
    assert frame.submit_button.clicked is False


def test_comeet_clicks_apply_button_to_reveal_the_iframe():
    frame = FakeComeetFrame(url="https://www.comeet.co/jobs/D5/1A/apply?token=x", fields={})
    page = FakeComeetPage(frame=frame, requires_click=True)
    comeet.apply(page, {"full_name": "Sean Larch", "email": "s@example.com"}, None, auto_submit=False)
    assert page._clicked is True


def test_comeet_needs_help_when_no_apply_form_is_found():
    page = FakeComeetPage(frame=None)
    result = comeet.apply(page, {"full_name": "Sean Larch", "email": "s@example.com"}, None, auto_submit=False)
    assert result["status"] == "needs_help"


def test_comeet_submits_when_auto_submit_true():
    frame = FakeComeetFrame(url="https://www.comeet.co/jobs/D5/1A/apply?token=x", fields={})
    page = FakeComeetPage(frame=frame)
    result = comeet.apply(page, {"full_name": "Sean Larch", "email": "s@example.com"}, None, auto_submit=True)
    assert result == {"status": "sent", "reason": None}
    assert frame.submit_button.clicked is True


# --- router -----------------------------------------------------------

def test_router_picks_greenhouse_by_url():
    assert pick_handler_by_url("https://job-boards.greenhouse.io/acme/jobs/1") is greenhouse


def test_router_picks_lever_by_url():
    assert pick_handler_by_url("https://jobs.lever.co/acme/xyz") is lever


def test_router_picks_comeet_by_url():
    assert pick_handler_by_url("https://www.comeet.com/jobs/acme/D5/pentester/1A") is comeet


def test_router_returns_none_for_unknown_ats():
    assert pick_handler_by_url("https://www.linkedin.com/jobs/view/12345") is None


def test_router_falls_back_to_page_content_for_embedded_comeet():
    page = FakeComeetPage(frame=FakeComeetFrame(url="https://www.comeet.co/jobs/D5/1A/apply?token=x"))
    handler = pick_handler("https://careers.example.com/openings/pentester", page=page)
    assert handler is comeet


def test_router_returns_none_when_page_has_no_comeet_widget_either():
    page = FakeComeetPage(frame=None)
    handler = pick_handler("https://careers.example.com/openings/pentester", page=page)
    assert handler is None

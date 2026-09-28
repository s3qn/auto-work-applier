"""Source parser tests. All run against saved HTML fixtures, no network."""

from __future__ import annotations

from pathlib import Path

from autojob.sources import alljobs, jobmaster, linkedin

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- linkedin ---------------------------------------------------------

def test_linkedin_parse_search_page_extracts_job_cards():
    jobs = linkedin.parse_search_page(_read("linkedin_search.html"))
    assert len(jobs) == 10
    first = jobs[0]
    assert first["source"] == "linkedin"
    assert first["title"] == "Penetration Tester"
    assert first["company"] == "Skyers"
    assert "Tel Aviv" in first["location"]
    assert first["external_id"] == "4470819037"
    assert first["url"].startswith("https://il.linkedin.com/jobs/view/penetration-tester-at-skyers")
    assert "?" not in first["url"]


def test_linkedin_parse_search_page_handles_empty_html():
    assert linkedin.parse_search_page("<html><body>no jobs here</body></html>") == []


def test_linkedin_parse_description_extracts_text():
    description = linkedin.parse_description(_read("linkedin_detail.html"))
    assert "Red Team Operator" in description
    assert len(description) > 100


# --- jobmaster ----------------------------------------------------------

def test_jobmaster_parse_search_page_extracts_job_cards():
    jobs = jobmaster.parse_search_page(_read("jobmaster_search.html"))
    assert len(jobs) >= 1
    first = jobs[0]
    assert first["source"] == "jobmaster"
    assert first["title"] == "SOC Engineer / Analyst"
    assert first["company"] == "MalamTeam"
    assert first["external_id"] == "9875892"
    assert first["url"] == "https://www.jobmaster.co.il/jobs/checknum.asp?key=9875892"


def test_jobmaster_slugify_joins_with_dashes():
    assert jobmaster._slugify("penetration tester") == "penetration-tester"


# --- alljobs --------------------------------------------------------------

def test_alljobs_parse_search_page_extracts_job_cards():
    jobs = alljobs.parse_search_page(_read("alljobs_search_relevant.html"))
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source"] == "alljobs"
    assert "Junior Penetration Tester" in job["title"]
    assert job["company"] == "CyberCo"
    assert job["external_id"] == "9001234"
    assert job["url"] == "https://www.alljobs.co.il/Search/UploadSingle.aspx?JobID=9001234"


def test_alljobs_looks_relevant_true_when_keyword_matches():
    jobs = alljobs.parse_search_page(_read("alljobs_search_relevant.html"))
    assert alljobs._looks_relevant(jobs, "Penetration Tester") is True


def test_alljobs_looks_relevant_false_for_generic_listing():
    jobs = alljobs.parse_search_page(_read("alljobs_search_irrelevant.html"))
    assert len(jobs) == 2
    assert alljobs._looks_relevant(jobs, "Penetration Tester") is False


class _FakePage:
    def __init__(self, html: str):
        self._html = html
        self.goto_calls: list[str] = []

    def goto(self, url, wait_until=None):
        self.goto_calls.append(url)

    def wait_for_selector(self, selector, timeout=None):
        return None

    def content(self):
        return self._html


class _FakeBrowserPageContext:
    def __init__(self, html: str):
        self.page = _FakePage(html)

    def __enter__(self):
        return self.page

    def __exit__(self, *a):
        return False


def _patch_browser_page(monkeypatch, html: str):
    monkeypatch.setattr(alljobs, "browser_page", lambda config, headless=True: _FakeBrowserPageContext(html))
    monkeypatch.setattr(alljobs, "_politeness_sleep", lambda: None)


def test_alljobs_search_raises_source_error_when_irrelevant(monkeypatch):
    from autojob.sources import SourceError

    _patch_browser_page(monkeypatch, _read("alljobs_search_irrelevant.html"))
    try:
        alljobs.search("Penetration Tester")
        assert False, "expected SourceError"
    except SourceError as exc:
        assert "didn't relate to the keyword" in str(exc)


def test_alljobs_search_returns_jobs_through_the_browser(monkeypatch):
    _patch_browser_page(monkeypatch, _read("alljobs_search_relevant.html"))
    jobs = alljobs.search("Penetration Tester")
    assert len(jobs) == 1
    assert jobs[0]["source"] == "alljobs"
    assert "Junior Penetration Tester" in jobs[0]["title"]


def test_alljobs_search_navigates_to_the_freetxt_url(monkeypatch):
    pages: list[_FakePage] = []

    def fake_browser_page(config, headless=True):
        ctx = _FakeBrowserPageContext(_read("alljobs_search_relevant.html"))
        pages.append(ctx.page)
        return ctx

    monkeypatch.setattr(alljobs, "browser_page", fake_browser_page)
    monkeypatch.setattr(alljobs, "_politeness_sleep", lambda: None)
    alljobs.search("Penetration Tester")
    assert len(pages) == 1
    assert "freetxt=Penetration" in pages[0].goto_calls[0]


def test_alljobs_search_raises_source_error_when_browser_is_busy(monkeypatch):
    from autojob.browser.context import BrowserBusyError
    from autojob.sources import SourceError

    def fake_browser_page(config, headless=True):
        raise BrowserBusyError("Another browser session is already running. Try again shortly.")

    monkeypatch.setattr(alljobs, "browser_page", fake_browser_page)
    try:
        alljobs.search("Penetration Tester")
        assert False, "expected SourceError"
    except SourceError as exc:
        assert "already running" in str(exc)

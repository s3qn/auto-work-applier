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


def test_alljobs_search_raises_source_error_when_irrelevant(monkeypatch):
    import httpx

    from autojob.sources import SourceError

    class FakeResponse:
        status_code = 200
        text = _read("alljobs_search_irrelevant.html")

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(alljobs, "_politeness_sleep", lambda: None)
    try:
        alljobs.search("Penetration Tester")
        assert False, "expected SourceError"
    except SourceError as exc:
        assert "ignores the keyword" in str(exc)

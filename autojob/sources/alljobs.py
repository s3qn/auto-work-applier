"""AllJobs (alljobs.co.il) guest search, driven through a real browser.

AllJobs' guest results page (`SearchResultsGuest.aspx`) renders job cards
server-side, but free-text search is not a plain query string the way it
looks: the visible search box is a React (MUI Autocomplete) widget, and
selecting a suggestion there fires a client-side request that lands on the
undocumented `freetxt` query parameter (not `freetext`, `keyword`, `q`, or
any of the other names tried while first building this scraper). Even with
the right parameter name, a plain HTTP GET (no JS execution) still returns a
short page with no job cards: the results only render once the page's own
JavaScript runs. Confirmed live on 2026-09-28: `httpx.get(...,
params={"freetxt": "Penetration Tester"})` returns a card-less page, while
loading the same URL in a real (even headless) browser and waiting for
`div.job-content-top` renders the actual filtered results.

So this module drives Playwright (see `autojob.browser.context`) instead of
httpx: navigate straight to the results URL with `freetxt` set, wait for the
job cards to render, then parse the same way the other sources do.

`_looks_relevant` stays as a safety net: if AllJobs' frontend ever changes
again and search silently falls back to a generic unfiltered listing, this
still catches it and raises `SourceError` (logged, run continues) instead of
feeding Sean irrelevant jobs.
"""

from __future__ import annotations

import re
import time
from random import uniform
from urllib.parse import quote

from selectolax.parser import HTMLParser

from autojob.browser.context import BrowserBusyError, browser_page
from autojob.config import load_config
from autojob.sources import SourceError

SEARCH_URL = "https://www.alljobs.co.il/SearchResultsGuest.aspx"
RESULTS_SELECTOR = "div.job-content-top"
# Matches both the "-highlight" (hot/premium listing) and plain card markup;
# both were seen live under the same search.
TITLE_SELECTOR = "div.job-content-top-title-highlight a, div.job-content-top-title a"
COMPANY_SELECTOR = "div.job-content-top-title-highlight div.T14 a, div.job-content-top-title div.T14 a"
MIN_DELAY = 1.0
MAX_DELAY = 2.0
RESULTS_TIMEOUT_MS = 15_000


def _politeness_sleep(sleep=time.sleep, rand=uniform) -> None:
    sleep(rand(MIN_DELAY, MAX_DELAY))


def _keyword_tokens(keyword: str) -> list[str]:
    return [tok.lower() for tok in re.split(r"\s+", keyword.strip()) if len(tok) > 2]


def parse_search_page(html: str) -> list[dict]:
    tree = HTMLParser(html)
    jobs: list[dict] = []
    for card in tree.css(RESULTS_SELECTOR):
        title_node = card.css_first(TITLE_SELECTOR)
        if title_node is None:
            continue
        href = title_node.attributes.get("href") or ""
        if not href:
            continue
        url = href if href.startswith("http") else f"https://www.alljobs.co.il{href}"
        company_node = card.css_first(COMPANY_SELECTOR)
        location_node = card.css_first("div.job-content-top-location")
        description_node = card.css_first("div.job-content-top-desc")
        job_id_match = re.search(r"JobID=(\d+)", href)
        jobs.append(
            {
                "source": "alljobs",
                "url": url,
                "title": title_node.text(strip=True),
                "company": company_node.text(strip=True) if company_node else "",
                "location": location_node.text(strip=True).replace("מיקום המשרה:", "").strip()
                if location_node
                else "",
                "description": description_node.text(strip=True) if description_node else "",
                "posted_at": None,
                "external_id": job_id_match.group(1) if job_id_match else None,
            }
        )
    return jobs


def _looks_relevant(jobs: list[dict], keyword: str) -> bool:
    tokens = _keyword_tokens(keyword)
    if not tokens or not jobs:
        return not jobs  # no jobs at all isn't evidence the search is broken
    haystacks = [f"{job['title']} {job['description']}".lower() for job in jobs]
    return any(token in haystack for token in tokens for haystack in haystacks)


def search(keyword: str) -> list[dict]:
    """Search AllJobs' guest listing for one keyword through a real browser.

    Raises SourceError if the browser can't be reached (already busy, no
    Chromium installed, navigation failure) or if the results plainly don't
    relate to the keyword, which is the symptom of the frontend's search
    breaking again. Both are caught and logged by the pipeline; this source
    never crashes a run.
    """
    url = f"{SEARCH_URL}?page=1&position=&type=&freetxt={quote(keyword)}&city=&region="
    try:
        config = load_config()
        with browser_page(config, headless=True) as page:
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(RESULTS_SELECTOR, timeout=RESULTS_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 - Playwright's own TimeoutError; zero results is fine
                pass
            html = page.content()
    except BrowserBusyError as exc:
        raise SourceError(f"alljobs: {exc}") from exc
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - Playwright can raise many exception types
        raise SourceError(f"alljobs: browser navigation failed ({exc})") from exc

    jobs = parse_search_page(html)
    if not _looks_relevant(jobs, keyword):
        raise SourceError(
            "alljobs: the results page rendered but didn't relate to the keyword "
            "at all, which is the symptom of AllJobs' frontend search breaking again. "
            "Skipping this source for this keyword rather than feeding Sean junk."
        )
    _politeness_sleep()
    return jobs

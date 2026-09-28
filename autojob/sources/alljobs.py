"""AllJobs (alljobs.co.il) guest search.

AllJobs' guest results page (`SearchResultsGuest.aspx`) renders job cards
server-side, but its free-text search box submits through an ASP.NET
postback (`__doPostBack` with viewstate), not a plain query string. While
building this scraper every plausible query parameter name was tried
(`freetext`, `keyword`, `keywords`, `text`, `q`, `searchtext`) against the
guest results page and all of them were silently ignored: the page always
renders the same generic "most recent" listing regardless of the keyword.

This module still issues the request (`freetext` is the most descriptive
guess and does no harm), but checks whether the returned cards plausibly
relate to the keyword. If none do, it raises SourceError so the caller logs
this source as blocked instead of feeding Sean irrelevant jobs.
"""

from __future__ import annotations

import random
import re
import time

import httpx
from selectolax.parser import HTMLParser

from autojob.sources import SourceError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SEARCH_URL = "https://www.alljobs.co.il/SearchResultsGuest.aspx"
MIN_DELAY = 2.0
MAX_DELAY = 4.0


def _politeness_sleep(sleep=time.sleep, rand=random.uniform) -> None:
    sleep(rand(MIN_DELAY, MAX_DELAY))


def _keyword_tokens(keyword: str) -> list[str]:
    return [tok.lower() for tok in re.split(r"\s+", keyword.strip()) if len(tok) > 2]


def parse_search_page(html: str) -> list[dict]:
    tree = HTMLParser(html)
    jobs: list[dict] = []
    for card in tree.css("div.job-content-top"):
        title_node = card.css_first("div.job-content-top-title-highlight a")
        if title_node is None:
            continue
        href = title_node.attributes.get("href") or ""
        if not href:
            continue
        url = href if href.startswith("http") else f"https://www.alljobs.co.il{href}"
        company_node = card.css_first("div.job-content-top-title-highlight div.T14 a")
        location_node = card.css_first("div.job-content-top-location")
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
                "description": "",
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
    """Search AllJobs' guest listing for one keyword.

    Raises SourceError if the site ignores the keyword entirely (see module
    docstring), which is the observed behavior for guest free-text search.
    """
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "he-IL,he;q=0.9,en;q=0.8"}
    params = {"page": "1", "position": "", "type": "", "city": "", "region": "", "freetext": keyword}
    try:
        with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
            response = client.get(SEARCH_URL, params=params)
            if response.status_code == 429:
                raise SourceError("alljobs: rate limited (HTTP 429)")
            response.raise_for_status()
            jobs = parse_search_page(response.text)
    except httpx.HTTPError as exc:
        raise SourceError(f"alljobs: request failed ({exc})") from exc
    if not _looks_relevant(jobs, keyword):
        raise SourceError(
            "alljobs: guest free-text search ignores the keyword and returns a generic "
            "listing (confirmed with several query param names). Needs the ASP.NET "
            "postback the site's search box actually uses, which this scraper doesn't "
            "implement. Skipping this source."
        )
    _politeness_sleep()
    return jobs

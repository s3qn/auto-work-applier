"""LinkedIn public guest job search. No login, no account involved.

Uses the same endpoints the LinkedIn jobs pages call for infinite scroll and
for the job detail pane: `jobs-guest/jobs/api/seeMoreJobPostings/search` for
result pages and `jobs-guest/jobs/api/jobPosting/<id>` for the full
description. Both were confirmed working with plain GET requests and a
desktop User-Agent while building this scraper.
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
SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
RESULTS_PER_PAGE = 10
MAX_PAGES = 2
MIN_DELAY = 2.0
MAX_DELAY = 4.0


def _politeness_sleep(sleep=time.sleep, rand=random.uniform) -> None:
    sleep(rand(MIN_DELAY, MAX_DELAY))


def _entity_id(card) -> str | None:
    urn = card.attributes.get("data-entity-urn") or ""
    match = re.search(r"jobPosting:(\d+)", urn)
    return match.group(1) if match else None


def parse_search_page(html: str) -> list[dict]:
    """Parse one page of LinkedIn guest search results into normalized jobs."""
    tree = HTMLParser(html)
    jobs: list[dict] = []
    for card in tree.css("div.base-card"):
        title_node = card.css_first("h3.base-search-card__title")
        link_node = card.css_first("a.base-card__full-link")
        if title_node is None or link_node is None:
            continue
        company_node = card.css_first("h4.base-search-card__subtitle")
        location_node = card.css_first("span.job-search-card__location")
        time_node = card.css_first("time")
        url = (link_node.attributes.get("href") or "").split("?")[0]
        if not url:
            continue
        jobs.append(
            {
                "source": "linkedin",
                "url": url,
                "title": title_node.text(strip=True),
                "company": company_node.text(strip=True) if company_node else "",
                "location": location_node.text(strip=True) if location_node else "",
                "description": "",
                "posted_at": time_node.attributes.get("datetime") if time_node else None,
                "external_id": _entity_id(card),
            }
        )
    return jobs


def parse_description(html: str) -> str:
    tree = HTMLParser(html)
    node = tree.css_first("div.description__text")
    if node is None:
        return ""
    return node.text(separator="\n", strip=True)


def search(
    keyword: str,
    *,
    location: str = "Israel",
    max_pages: int = MAX_PAGES,
    fetch_descriptions: bool = True,
    max_detail_fetches: int | None = None,
) -> list[dict]:
    """Search LinkedIn's public guest job listings for one keyword.

    Stops paging after `max_pages` or when a page returns no cards. Raises
    SourceError on a blocked or failed request so the caller can log it and
    keep going with other sources.
    """
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    jobs: list[dict] = []
    try:
        with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
            for page in range(max_pages):
                start = page * RESULTS_PER_PAGE
                params = {
                    "keywords": keyword,
                    "location": location,
                    "f_TPR": "r86400",
                    "start": start,
                }
                response = client.get(SEARCH_URL, params=params)
                if response.status_code == 429:
                    raise SourceError("linkedin: rate limited (HTTP 429), backing off")
                response.raise_for_status()
                page_jobs = parse_search_page(response.text)
                if not page_jobs:
                    break
                jobs.extend(page_jobs)
                if page + 1 < max_pages:
                    _politeness_sleep()

            if fetch_descriptions:
                fetched = 0
                for job in jobs:
                    if max_detail_fetches is not None and fetched >= max_detail_fetches:
                        break
                    if not job["external_id"]:
                        continue
                    _politeness_sleep()
                    try:
                        detail = client.get(DETAIL_URL.format(job_id=job["external_id"]))
                    except httpx.HTTPError:
                        continue
                    fetched += 1
                    if detail.status_code == 200:
                        job["description"] = parse_description(detail.text)
    except httpx.HTTPError as exc:
        raise SourceError(f"linkedin: request failed ({exc})") from exc
    return jobs

"""JobMaster (jobmaster.co.il) guest search.

Anonymous users only see the first page of results, so this fetches one
page per keyword: `https://www.jobmaster.co.il/jobs/q-<slugified keyword>/`.
That path form (not a `?freetext=` query param, which the site ignores) was
confirmed working while building this scraper: it renders job cards
server-side with no login required.
"""

from __future__ import annotations

import random
import time
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from autojob.sources import SourceError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
BASE_URL = "https://www.jobmaster.co.il"
MIN_DELAY = 2.0
MAX_DELAY = 4.0


def _politeness_sleep(sleep=time.sleep, rand=random.uniform) -> None:
    sleep(rand(MIN_DELAY, MAX_DELAY))


def _slugify(keyword: str) -> str:
    return quote("-".join(keyword.strip().split()))


def parse_search_page(html: str) -> list[dict]:
    tree = HTMLParser(html)
    jobs: list[dict] = []
    for card in tree.css("article.JobItem"):
        title_node = card.css_first("a.CardHeader")
        if title_node is None:
            continue
        href = title_node.attributes.get("href") or ""
        if not href:
            continue
        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        company_node = card.css_first("a.CompanyNameLink")
        location_node = card.css_first("li.jobLocation a")
        description_node = card.css_first("div.jobShortDescription")
        external_id = card.attributes.get("id") or ""
        external_id = external_id[len("misra") :] if external_id.startswith("misra") else external_id
        jobs.append(
            {
                "source": "jobmaster",
                "url": url,
                "title": title_node.text(strip=True),
                "company": company_node.text(strip=True) if company_node else "",
                "location": location_node.text(strip=True) if location_node else "",
                "description": description_node.text(strip=True) if description_node else "",
                "posted_at": None,
                "external_id": external_id or None,
            }
        )
    return jobs


def search(keyword: str) -> list[dict]:
    """Search JobMaster's guest listing (first page only) for one keyword."""
    url = f"{BASE_URL}/jobs/q-{_slugify(keyword)}/"
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "he-IL,he;q=0.9,en;q=0.8"}
    try:
        with httpx.Client(headers=headers, timeout=20.0, follow_redirects=True) as client:
            response = client.get(url)
            if response.status_code == 429:
                raise SourceError("jobmaster: rate limited (HTTP 429)")
            response.raise_for_status()
            jobs = parse_search_page(response.text)
    except httpx.HTTPError as exc:
        raise SourceError(f"jobmaster: request failed ({exc})") from exc
    _politeness_sleep()
    return jobs

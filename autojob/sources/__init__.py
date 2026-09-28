"""Job board scrapers.

Every source module exposes a `search(keyword: str) -> list[dict]` function
that returns normalized job dicts with these keys:

    source       str, the source name ("linkedin", "alljobs", "jobmaster")
    url          str, the canonical job posting URL
    title        str
    company      str
    location     str, may be empty
    description  str, may be empty if not available from the search page
    posted_at    str or None, ISO date if known
    external_id  str or None, the source's own id for the posting

A source that cannot be reached, is blocked, or whose page format no
longer matches raises `SourceError` with a human-readable reason. Callers
are expected to catch it, log it, and continue with the other sources.
"""

from __future__ import annotations


class SourceError(Exception):
    """Raised when a source cannot be scraped. The message is log-ready."""

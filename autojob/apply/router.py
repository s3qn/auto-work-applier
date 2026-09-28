"""Picks the right apply handler for a job's application URL (and, for a
Comeet widget embedded on a company's own domain, the loaded page itself).
"""

from __future__ import annotations

from autojob.apply.handlers import comeet, greenhouse, lever

HANDLERS = (greenhouse, lever, comeet)


def pick_handler_by_url(url: str):
    """Return the handler module that recognizes `url`, or None."""
    for handler in HANDLERS:
        if handler.matches(url):
            return handler
    return None


def pick_handler(url: str, page=None):
    """Return the handler module for this job, checking the URL first and,
    if nothing matches and a live `page` is given, falling back to a
    page-content check (currently only Comeet needs this, for its embedded
    widget case). Returns None for an ATS this build doesn't handle yet."""
    handler = pick_handler_by_url(url)
    if handler is not None:
        return handler
    if page is not None and comeet.matches_page(page):
        return comeet
    return None

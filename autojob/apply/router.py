"""Picks the right apply handler for a job's application URL (and, for a
Comeet widget embedded on a company's own domain, the loaded page itself).

Greenhouse, Lever and Comeet are deterministic, confirmed-selector handlers
for the ATSes this build knows well. Everything else, company career pages
(Cato Networks, Akamai, Top Soft, Malam, Matrix, lema.ai, Snatch-Up and
others), AllJobs' logged-in "send CV" flow, and LinkedIn listings, goes to
`handlers/generic.py`, a browser-plus-LLM handler that actually opens the
page and tries, rather than escalating to `needs_help` without an attempt.
"""

from __future__ import annotations

from autojob.apply.handlers import comeet, generic, greenhouse, lever

HANDLERS = (greenhouse, lever, comeet)


def pick_handler_by_url(url: str):
    """Return the ATS-specific handler that recognizes `url`, or None. Does
    not consider the generic fallback: this is "is this a known ATS", used
    both by `pick_handler` and by the generic handler itself to notice when
    following a redirect landed on a domain a specific handler already
    covers."""
    for handler in HANDLERS:
        if handler.matches(url):
            return handler
    return None


def pick_handler(url: str, page=None):
    """Return the handler module for this job: an ATS-specific handler when
    one recognizes the URL or (for an embedded Comeet widget with no URL
    marker to go on) the loaded page, otherwise `handlers/generic.py`. Never
    returns None: the generic handler is the catch-all for every ATS this
    build has no dedicated handler for."""
    handler = pick_handler_by_url(url)
    if handler is not None:
        return handler
    if page is not None and comeet.matches_page(page):
        return comeet
    return generic
